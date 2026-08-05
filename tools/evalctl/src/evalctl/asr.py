"""음성인식 품질 지표 — WER / CER.

지금까지 골든셋은 **텍스트 경로만** 덮었다. 검색·마스킹·읽기·근거·의도는
숫자가 있는데 정작 제품의 첫 관문인 음성인식에는 숫자가 하나도 없었다.
"잘 되는 것 같다"로 남아 있던 유일한 자리다.

**한국어에서는 CER이 WER보다 정직하다.** 한국어는 띄어쓰기가 흔들려서
어절 단위 WER이 실력과 무관하게 출렁인다 — "회의 록"과 "회의록"은 같은 말인데
WER은 2배로 틀렸다고 말한다. 그래서 둘 다 내되 **기준선은 CER로 잡는다**.

정규화도 같은 이유다. 구두점·대소문자·중복 공백은 인식 실패가 아니라 표기
차이다. 그걸 오류로 세면 모델을 바꿔도 숫자가 안 움직인다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 전각 따옴표는 한국어 원문에 실제로 나온다 — 오타가 아니라 처리 대상이다.
_PUNCT = re.compile(
    "[" + re.escape(".,!?;:~…·\"'()[]{}<>/\\|`^*_+=-" + "\u201c\u201d\u2018\u2019") + "]"
)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """비교 전 표기 차이를 걷어낸다.

    NFC로 모으는 것이 먼저다. 자모가 분리된 문자열(NFD)은 눈에 같아 보여도
    글자 수가 달라 CER이 통째로 어긋난다 — macOS에서 만든 정답 파일이
    이 모양으로 오는 일이 잦다.
    """
    text = unicodedata.normalize("NFC", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip().lower()


def _edit_distance(a: list[str], b: list[str]) -> int:
    """Levenshtein. 두 줄만 들고 돈다 — 긴 녹취에서 전체 행렬은 메모리를 먹는다."""
    if not a:
        return len(b)
    previous = list(range(len(a) + 1))
    for j, bj in enumerate(b, start=1):
        current = [j]
        for i, ai in enumerate(a, start=1):
            # 일치하면 대각선(d[i-1][j-1])을 그대로 물려받는다.
            # previous[i] 는 d[i][j-1] 이라 한 칸 어긋난다 — 그러면 모든 치환이
            # 두 번 세어져 CER 이 실제의 두 배로 나온다.
            current.append(
                previous[i - 1]
                if ai == bj
                else 1 + min(previous[i], current[i - 1], previous[i - 1])
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class AsrOutcome:
    """한 발화의 결과. 비율이 아니라 **개수**를 들고 다닌다.

    사례별 비율을 평균 내면 짧은 발화가 과대 대표된다("네" 한 글자를 틀리면
    CER 100%). 코퍼스 전체의 오류 합/정답 길이 합으로 내야 맞다.
    """

    case_id: str
    reference: str
    hypothesis: str
    word_errors: int
    word_total: int
    char_errors: int
    char_total: int
    latency_ms: float = 0.0

    @property
    def empty(self) -> bool:
        """인식이 아무것도 못 냈다. 오류율만 보면 '전부 틀림'과 구분되지 않는다."""
        return not self.hypothesis.strip()


def score(case_id: str, reference: str, hypothesis: str, latency_ms: float = 0.0) -> AsrOutcome:
    ref, hyp = normalize(reference), normalize(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    ref_chars, hyp_chars = list(ref.replace(" ", "")), list(hyp.replace(" ", ""))
    return AsrOutcome(
        case_id=case_id,
        reference=reference,
        hypothesis=hypothesis,
        word_errors=_edit_distance(ref_words, hyp_words),
        word_total=len(ref_words),
        char_errors=_edit_distance(ref_chars, hyp_chars),
        char_total=len(ref_chars),
        latency_ms=latency_ms,
    )


@dataclass(frozen=True)
class AsrMetrics:
    total: int
    wer: float
    cer: float
    empty: int
    p95_latency_ms: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "wer": self.wer,
            "cer": self.cer,
            "empty": self.empty,
            "p95_latency_ms": self.p95_latency_ms,
        }


def summarize_asr(outcomes: list[AsrOutcome]) -> AsrMetrics:
    if not outcomes:
        return AsrMetrics(total=0, wer=0.0, cer=0.0, empty=0, p95_latency_ms=0.0)

    word_errors = sum(o.word_errors for o in outcomes)
    word_total = sum(o.word_total for o in outcomes)
    char_errors = sum(o.char_errors for o in outcomes)
    char_total = sum(o.char_total for o in outcomes)

    from evalctl.metrics import percentile

    return AsrMetrics(
        total=len(outcomes),
        wer=round(word_errors / word_total, 3) if word_total else 0.0,
        cer=round(char_errors / char_total, 3) if char_total else 0.0,
        empty=sum(1 for o in outcomes if o.empty),
        p95_latency_ms=percentile([o.latency_ms for o in outcomes], 0.95),
    )
