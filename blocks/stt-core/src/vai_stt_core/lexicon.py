"""커스텀 사전 인식 교정.

범용 STT는 "무배당 행복플러스보험" 같은 고유명사를 거의 확실히 틀린다.
틀린 인식은 그 자리에서 끝나지 않는다 — 검색 질의도 요약도 함께 어긋난다.

두 지점에서 개입한다:

1. **디코딩 편향** — Whisper 계열의 ``initial_prompt``에 정답 표기를 넣어
   모델이 그 표기를 선호하게 만든다. 공짜에 가깝지만 보장은 없다.
2. **후처리 교정** — 관측된 오인식 표기를 정답으로 치환한다. 확실하지만
   등록된 변형만 잡는다.

둘 다 필요하다. 1번은 미등록 변형에도 도움이 되고, 2번은 1번이 실패했을 때의
안전망이다.

띄어쓰기는 STT가 가장 자주 틀리는 부분이라, 매칭 시 공백을 유연하게 다룬다.
"무배당행복플러스보험"과 "무배당 행복 플러스 보험"이 같은 것으로 취급되어야 한다.
"""

from __future__ import annotations

import logging
import re
import time

from vai_common.config_store import CachedConfig
from vai_contracts.authoring import Lexicon, LexiconEntry

log = logging.getLogger(__name__)

MAX_ENTRIES = 500
"""실시간 경로에 걸리므로 상한을 둔다. 초과분은 무시하고 경고한다 —
조용히 자르면 운영자는 등록했는데 왜 안 되는지 알 수 없다."""

MAX_PROMPT_CHARS = 200
"""Whisper initial_prompt 상한. 너무 길면 디코딩 품질이 오히려 떨어진다."""

_SPACE = re.compile(r"\s+")


def _flexible_pattern(text: str) -> str:
    """글자 사이 공백을 허용하는 패턴으로 만든다.

    "무배담 행복플러스" → ``무\\s*배\\s*담\\s*행\\s*복\\s*플\\s*러\\s*스``
    STT는 띄어쓰기를 자주 틀리므로, 공백만 다른 경우를 놓치면 사전이 거의 무용해진다.
    """
    chars = [re.escape(char) for char in _SPACE.sub("", text)]
    return r"\s*".join(chars)


class LexiconCorrector:
    """사전 기반 후처리 교정기.

    사전 버전이 바뀔 때만 패턴을 다시 컴파일한다. 발화마다 컴파일하면
    지연 예산을 정규식 컴파일에 쓰게 된다.
    """

    def __init__(self, lexicon: Lexicon | None = None) -> None:
        self._version = -1
        self._pattern: re.Pattern[str] | None = None
        self._lookup: dict[str, str] = {}
        self._prompt = ""
        if lexicon is not None:
            self.update(lexicon)

    @property
    def version(self) -> int:
        return self._version

    @property
    def initial_prompt(self) -> str:
        """STT 어댑터에 넘길 디코딩 힌트."""
        return self._prompt

    @property
    def rule_count(self) -> int:
        return len(self._lookup)

    def update(self, lexicon: Lexicon) -> None:
        """사전이 바뀌었을 때 패턴을 재구성한다."""
        if lexicon.version == self._version:
            return

        entries = [entry for entry in lexicon.entries if entry.enabled and entry.surface.strip()]
        if len(entries) > MAX_ENTRIES:
            log.warning(
                "사전 항목이 상한을 넘어 일부만 적용한다",
                extra={"total": len(entries), "applied": MAX_ENTRIES},
            )
            entries = entries[:MAX_ENTRIES]

        self._pattern, self._lookup = _build_matcher(entries)
        self._prompt = _build_prompt(entries)
        self._version = lexicon.version
        log.info(
            "사전 적용",
            extra={
                "version": lexicon.version,
                "entries": len(entries),
                "rules": len(self._lookup),
            },
        )

    def correct(self, text: str) -> str:
        """인식 결과를 교정한다. 사전이 비어 있으면 원문을 그대로 돌려준다.

        **단일 패스**로 치환한다. 규칙을 순서대로 적용하면 앞 규칙이 만든
        결과가 뒤 규칙의 입력이 되어, "무배당 행복플러스보험"이 다시
        "행복플러스" 규칙에 걸려 "…보험보험"이 된다.
        """
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(self._replace, text)

    def _replace(self, match: re.Match[str]) -> str:
        # 패턴이 공백을 유연하게 먹으므로, 정규화한 형태로 되찾는다.
        normalized = _SPACE.sub("", match.group(0))
        return self._lookup.get(normalized, match.group(0))


def _build_matcher(
    entries: list[LexiconEntry],
) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    """모든 표기를 하나의 교체 패턴으로 합친다.

    긴 표기를 앞에 둔다. 파이썬 정규식의 ``|``는 왼쪽 우선이라, 같은 위치에서
    긴 쪽이 먼저 시도되어 "행복플러스"가 "무배당 행복플러스보험"을 가로채지 않는다.
    """
    lookup: dict[str, str] = {}
    candidates: list[str] = []

    for entry in entries:
        surface = entry.surface.strip()
        # 정답 표기 자체도 공백 변형으로 들어올 수 있으므로 후보에 포함한다.
        for variant in [surface, *entry.variants]:
            normalized = _SPACE.sub("", variant.strip())
            if len(normalized) < 2 or normalized in lookup:
                continue
            lookup[normalized] = surface
            candidates.append(normalized)

    if not candidates:
        return None, {}

    candidates.sort(key=len, reverse=True)
    combined = "|".join(_flexible_pattern(candidate) for candidate in candidates)
    return re.compile(combined), lookup


def _build_prompt(entries: list[LexiconEntry]) -> str:
    """디코딩 편향용 힌트 문장.

    상한을 넘으면 앞쪽 항목만 넣는다. 운영자가 중요한 용어를 위에 두면
    그것이 우선 반영되는 셈이라, 임의 절단보다 예측 가능하다.
    """
    parts: list[str] = []
    length = 0
    for entry in entries:
        surface = entry.surface.strip()
        if length + len(surface) + 2 > MAX_PROMPT_CHARS:
            break
        parts.append(surface)
        length += len(surface) + 2
    return ", ".join(parts)


def measure_correction(corrector: LexiconCorrector, text: str) -> tuple[str, float]:
    """교정 결과와 소요 시간(ms). 지연 예산 회귀 감시용."""
    started = time.perf_counter()
    corrected = corrector.correct(text)
    return corrected, (time.perf_counter() - started) * 1000


class LexiconCache:
    """테넌트별 교정기 캐시.

    사전은 배포 채널(:mod:`vai_common.config_store`)에서 읽는다. 저작 도구를
    직접 부르지 않으므로, 저작 블록이 꺼져 있어도 마지막 배포본으로 계속 돈다.
    """

    def __init__(self, configs: CachedConfig[Lexicon]) -> None:
        self._configs = configs
        self._correctors: dict[str, LexiconCorrector] = {}

    async def corrector(self, tenant_id: str) -> LexiconCorrector | None:
        lexicon = await self._configs.get(tenant_id)
        if lexicon is None:
            return None
        corrector = self._correctors.get(tenant_id)
        if corrector is None:
            corrector = LexiconCorrector(lexicon)
            self._correctors[tenant_id] = corrector
        else:
            # 버전이 같으면 재컴파일하지 않는다.
            corrector.update(lexicon)
        return corrector

    def invalidate(self, tenant_id: str | None = None) -> None:
        self._configs.invalidate(tenant_id)
