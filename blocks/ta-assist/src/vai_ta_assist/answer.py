"""추천 답변 생성 — 근거 없이는 한 글자도 만들지 않는다.

상담원 화면에 뜬 문장은 상담원이 그대로 읽는다. 그러니 여기서 지어낸 숫자는
곧바로 고객에게 전달되고, 금융 상담에서 그것은 불완전판매다. **틀린 답변은
답변이 없는 것보다 나쁘다** — 근거 원문만 보여주면 상담원이 직접 읽고 판단하지만,
그럴듯한 오답은 그 판단 자체를 건너뛰게 만든다.

그래서 생성된 문장을 그대로 믿지 않고 **검증을 통과한 것만** 내보낸다:

1. **인용 표기**가 있어야 한다. 어느 조항을 근거로 말하는지 없으면 상담원이
   확인할 방법이 없다.
2. **답변의 모든 숫자가 인용된 근거에 있어야 한다.** 환각이 가장 자주, 가장
   위험하게 나타나는 자리가 숫자다("수수료 3,000원"이 "5,000원"으로 바뀌는 것).
   근거에 없는 숫자가 하나라도 있으면 통째로 버린다.
3. **어휘가 근거에 얼마나 걸쳐 있는지** 본다. 숫자만 보면 "홈페이지에서도
   확인하실 수 있습니다" 같은, 근거에 없는 문장을 통째로 지어내도 통과한다.
4. **확약 표현**을 쓰지 않아야 한다. "반드시 면제됩니다" 같은 문장은 약관이
   조건부로 쓴 내용을 무조건으로 바꿔 놓는다.

셋 중 하나라도 어긋나면 답변을 버리고 근거 원문만 남긴다. 버린 이유는 로그에
남겨 두어 사전·프롬프트를 고칠 근거로 쓴다 — 조용히 버리면 왜 답변이 안 뜨는지
아무도 설명하지 못한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from vai_contracts.retrieval import ScoredChunk
from vai_retrieval.tokenize import tokenize
from vai_ta_assist.ports import CompletePort

log = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 140
"""상담원이 통화 중에 읽을 수 있는 분량 — 한국어 두 문장 정도다.

넘으면 안 읽히고, 안 읽히면 없는 것과 같다. 오히려 화면만 가려 근거 원문까지
못 보게 만든다. 근거를 길게 옮겨 적은 답변은 요약이 아니라 복사다."""

MIN_COVERAGE = 0.35
"""답변 어휘가 인용 근거에 걸쳐 있어야 하는 최소 비율.

**의도적으로 느슨하다.** 한국어 존대 변환("지정할 수 있다"→"지정하실 수 있습니다")
만으로도 표면 토큰이 상당히 달라지므로, 빡빡하게 잡으면 멀쩡한 답변을 계속
버리게 된다. 이 검사가 노리는 것은 문장 단위 미세 왜곡이 아니라 **근거에 없는
내용을 통째로 지어낸 경우**다(실측: 정상 답변 0.50~0.76, 지어낸 문장 0.24).

문장 단위 함의(entailment) 판정은 NLI 모델이 필요하고, 그건 이 블록의 지연
예산 밖이다 — 알려진 한계로 남긴다."""

MIN_COVERAGE_TOKENS = 20
"""이 개수 미만의 답변에는 어휘 검사를 걸지 않는다.

"연회비는 면제하실 수 있습니다"처럼 짧은 문장은 토큰이 열 개 남짓이라 존대
어미('습니', '니다', '하실')가 분모의 상당 부분을 차지한다. 그 상태로 비율을
재면 근거에 충실한 답변도 0.25가 나온다 — **검사가 아니라 난수가 된다.**
짧은 답변은 지어낼 여지도 그만큼 작고, 인용·숫자 검사는 길이와 무관하게 걸린다."""

SYSTEM_PROMPT = (
    "너는 콜센터 상담원을 돕는 답변 작성기다. 아래 [근거] 안의 내용만 사용해 "
    "고객 질문에 대한 답변을 2문장 이내로 쓴다.\n"
    "규칙:\n"
    "1. 근거에 없는 내용·숫자는 절대 쓰지 않는다.\n"
    "2. 문장 끝에 근거 번호를 [1] 형태로 표기한다.\n"
    "3. 근거로 답할 수 없으면 정확히 '답변불가'라고만 쓴다.\n"
    "4. '반드시', '무조건' 같은 확약 표현을 쓰지 않는다."
)

REFUSAL = "답변불가"
"""모델이 근거 부족을 스스로 밝히는 신호. 이 경우는 실패가 아니라 정상 동작이다."""

# 약관은 대부분 조건부로 쓰여 있다. 확약으로 바꿔 읽어 주면 그 자체가 분쟁 소재다.
OVERPROMISE = ("반드시", "무조건", "100%", "절대적으로", "언제나 가능", "항상 가능")

_CITATION = re.compile(r"\[(\d+)\]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass(frozen=True)
class Evidence:
    """번호가 붙은 근거 한 건. 번호가 곧 인용 표기다."""

    index: int
    doc_id: str
    title: str
    text: str


@dataclass(frozen=True)
class AnswerVerdict:
    """생성 결과와 판정.

    ``answer``가 비어 있으면 화면에는 근거 원문만 뜬다. ``reason``은 왜 버렸는지다.
    """

    answer: str = ""
    citations: list[int] = field(default_factory=list)
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return bool(self.answer)


def build_evidence(hits: list[ScoredChunk]) -> list[Evidence]:
    return [
        Evidence(
            index=i + 1,
            doc_id=hit.chunk.doc_id,
            title=hit.chunk.title,
            text=hit.chunk.text.strip(),
        )
        for i, hit in enumerate(hits)
    ]


def render_prompt(query: str, evidence: list[Evidence]) -> str:
    blocks = "\n".join(f"[{item.index}] {item.title}\n{item.text}" for item in evidence)
    return f"[근거]\n{blocks}\n\n[고객 질문]\n{query}\n\n[답변]"


def numbers_in(text: str) -> set[str]:
    """숫자를 비교 가능한 형태로 뽑는다.

    천 단위 쉼표는 표기 차이일 뿐이라 지운다 — "3,000"과 "3000"을 다른 숫자로
    보면 멀쩡한 답변을 환각으로 오판한다. 반대로 소수점은 남긴다(1.0%와 10%는
    완전히 다른 수수료다).
    """
    return {match.group().replace(",", "").rstrip(".") for match in _NUMBER.finditer(text)}


def lexical_coverage(body: str, used: list[Evidence]) -> float | None:
    """답변 토큰 중 인용 근거에도 나타나는 비율. 잴 수 없으면 ``None``.

    검색과 **같은 토크나이저**를 쓴다. 음절 바이그램이 섞여 있어 조사·어미
    변형을 어느 정도 흡수하는데, 그 성질이 여기서도 그대로 필요하다.

    토큰이 :data:`MIN_COVERAGE_TOKENS` 미만이면 ``None``을 돌려준다 — 억지로
    숫자를 내놓는 것보다 "못 잰다"고 말하는 편이 낫다.
    """
    answer_tokens = set(tokenize(body))
    if len(answer_tokens) < MIN_COVERAGE_TOKENS:
        return None
    evidence_tokens = set(tokenize(" ".join(item.text for item in used)))
    return len(answer_tokens & evidence_tokens) / len(answer_tokens)


def verify(text: str, evidence: list[Evidence]) -> AnswerVerdict:
    """생성된 문장을 검증한다. 통과한 것만 답변이 된다."""
    cleaned = text.strip()
    if not cleaned:
        return AnswerVerdict(reason="빈 응답")
    if REFUSAL in cleaned:
        # 모델이 스스로 근거 부족을 밝힌 경우. 이것은 올바른 동작이다.
        return AnswerVerdict(reason="근거 부족(모델 자진 거부)")

    cited = [int(match) for match in _CITATION.findall(cleaned)]
    valid_indexes = {item.index for item in evidence}
    if not cited:
        return AnswerVerdict(reason="인용 표기 없음")
    unknown = sorted(set(cited) - valid_indexes)
    if unknown:
        # 없는 근거를 인용하는 것은 환각의 가장 노골적인 형태다.
        return AnswerVerdict(reason=f"존재하지 않는 근거 인용 {unknown}")

    body = _CITATION.sub(" ", cleaned).strip()
    if len(body) > MAX_ANSWER_CHARS:
        return AnswerVerdict(reason=f"길이 초과 {len(body)}자")

    used = [item for item in evidence if item.index in set(cited)]
    grounded = set[str]().union(*(numbers_in(item.text) for item in used)) if used else set()
    invented = sorted(numbers_in(body) - grounded)
    if invented:
        return AnswerVerdict(reason=f"근거에 없는 숫자 {invented}")

    over = [word for word in OVERPROMISE if word in body]
    if over:
        return AnswerVerdict(reason=f"확약 표현 {over}")

    # 가장 거친 검사를 마지막에 둔다. 앞의 검사들이 더 구체적인 이유를 알려주고,
    # 그 이유가 프롬프트를 고칠 근거가 된다.
    coverage = lexical_coverage(body, used)
    if coverage is not None and coverage < MIN_COVERAGE:
        return AnswerVerdict(reason=f"근거 밖 내용 (어휘 일치 {coverage:.2f})")

    return AnswerVerdict(answer=cleaned, citations=sorted(set(cited)))


class AnswerComposer:
    """근거 → 추천 답변. LLM이 없으면 아무 답변도 만들지 않는다.

    폴백으로 근거 문장을 잘라 붙이는 방법도 있지만 하지 않는다. 잘린 조항은
    조건절이 떨어져 나가 뜻이 바뀌는 일이 잦고, 그것은 근거를 보여주는 것보다
    위험하다.
    """

    def __init__(self, complete: CompletePort | None = None) -> None:
        self._complete = complete

    @property
    def enabled(self) -> bool:
        return self._complete is not None

    async def compose(self, query: str, hits: list[ScoredChunk]) -> AnswerVerdict:
        if self._complete is None:
            return AnswerVerdict(reason="생성기 미구성")
        evidence = build_evidence(hits)
        if not evidence:
            # 근거가 없으면 생성하지 않는다. 이 규칙에는 예외를 두지 않는다.
            return AnswerVerdict(reason="근거 없음")

        try:
            raw = await self._complete(render_prompt(query, evidence), SYSTEM_PROMPT)
        except Exception:
            log.warning("추천 답변 생성 실패 — 근거 원문만 표시", exc_info=True)
            return AnswerVerdict(reason="생성 호출 실패")

        verdict = verify(str(raw), evidence)
        if not verdict.accepted:
            log.info(
                "추천 답변 폐기",
                extra={"reason": verdict.reason, "query": query},
            )
        return verdict
