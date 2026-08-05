"""TTS 읽기 사전 캐시.

전처리 규칙(:mod:`vai_tts_core.normalize`)은 숫자·날짜·단위처럼 **규칙으로
정해지는 것**을 다룬다. 고유명사와 사내 용어는 규칙이 없다 —
"무배당행복플러스"를 어떻게 읽을지는 그 회사만 안다.

그걸 코드로 받으면 안내 문구 하나에 공급사 배포가 필요해진다. 사전은 저작
화면에서 등록하고 배포 채널로 흘린다.

**저작 블록이 꺼져 있어도 마지막 배포본으로 계속 돈다.** 실시간 합성 경로가
저작 도구의 가용성에 묶이면, 콘솔이 죽는 날 봇도 함께 이상해진다.
"""

from __future__ import annotations

import logging

from vai_common.config_store import CachedConfig
from vai_contracts.authoring import TtsLexicon

log = logging.getLogger(__name__)

MAX_READINGS = 500
"""실시간 경로에 걸리므로 상한을 둔다. 초과분은 무시하되 **경고를 남긴다** —
조용히 자르면 운영자는 등록했는데 왜 안 읽히는지 알 수 없다."""


def to_mapping(lexicon: TtsLexicon) -> dict[str, str]:
    """사전을 치환표로. 꺼진 항목과 빈 항목은 버린다."""
    readings: dict[str, str] = {}
    for entry in lexicon.readings:
        if not entry.enabled or not entry.surface.strip() or not entry.reading.strip():
            continue
        if len(readings) >= MAX_READINGS:
            log.warning(
                "TTS 읽기 사전이 상한을 넘었다 — 초과분은 적용하지 않는다",
                extra={"tenant_id": lexicon.tenant_id, "limit": MAX_READINGS},
            )
            break
        readings[entry.surface.strip()] = entry.reading.strip()
    return readings


class ReadingCache:
    """테넌트별 치환표 캐시.

    버전이 그대로면 다시 만들지 않는다. 합성마다 사전을 재구성하면 그 자체가
    첫 소리 지연이 된다.
    """

    def __init__(self, configs: CachedConfig[TtsLexicon]) -> None:
        self._configs = configs
        self._cache: dict[str, tuple[int, dict[str, str]]] = {}

    async def readings(self, tenant_id: str) -> dict[str, str]:
        lexicon = await self._configs.get(tenant_id)
        if lexicon is None:
            return {}
        cached = self._cache.get(tenant_id)
        if cached is not None and cached[0] == lexicon.version:
            return cached[1]
        mapping = to_mapping(lexicon)
        self._cache[tenant_id] = (lexicon.version, mapping)
        return mapping

    def invalidate(self, tenant_id: str | None = None) -> None:
        self._configs.invalidate(tenant_id)
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)
