"""요약 프롬프트 — 버전이 붙은 리소스.

프롬프트를 코드 문자열로 흩어 두면 "왜 지난주보다 요약이 나빠졌지"에 답할 수
없다. 여기 모아 버전을 매기고, 산출물에 버전을 기록한다(docs/06 §2-5).

프롬프트 변경 시 반드시 버전을 올린다. 같은 버전으로 내용만 바꾸면 추적이 끊긴다.
"""

from __future__ import annotations

from dataclasses import dataclass

PROMPT_VERSION = "aicc-v1/meeting-v1"

AICC_SYSTEM = """너는 금융 콜센터의 상담 기록 작성 보조다.
주어진 상담 녹취록을 표준 양식으로 요약한다.

규칙:
- 녹취록에 없는 내용을 지어내지 않는다. 모르면 빈 문자열로 둔다.
- [RRN_MASKED] 같은 마스킹 토큰은 그대로 두고 복원을 시도하지 않는다.
- 금액·기간·조건은 녹취록에 명시된 값만 쓴다.
- 반드시 아래 JSON 형식으로만 답한다. 설명을 덧붙이지 않는다.

{
  "category": "상담 대분류",
  "subcategory": "소분류",
  "customer_request": "고객 요청 요약 (1~2문장)",
  "agent_response": "상담원 안내 요약 (1~2문장)",
  "resolution": "처리 결과",
  "follow_up": "후속 조치 (없으면 빈 문자열)",
  "keywords": ["핵심어", "3~5개"]
}"""

MEETING_SYSTEM = """너는 회의록 작성 보조다.
주어진 회의 녹취록에서 안건·결정사항·실행항목을 정리한다.

규칙:
- 녹취록에 없는 내용을 지어내지 않는다.
- 담당자와 기한은 녹취록에서 명시적으로 언급된 경우에만 채운다.
  추측한 날짜를 넣지 않는다 — 잘못된 기한은 없느니만 못하다.
- 반드시 아래 JSON 형식으로만 답한다.

{
  "title": "회의 제목",
  "agenda": ["안건1", "안건2"],
  "decisions": ["결정사항1"],
  "action_items": [
    {"text": "실행항목", "owner": "담당자 또는 빈 문자열", "due": "기한 또는 빈 문자열"}
  ],
  "participants": ["참석자"]
}"""


@dataclass(frozen=True)
class PromptSpec:
    version: str
    system: str


AICC = PromptSpec("aicc-v1", AICC_SYSTEM)
MEETING = PromptSpec("meeting-v1", MEETING_SYSTEM)


def build_user_prompt(transcript: str, *, max_chars: int = 12_000) -> str:
    """녹취록을 프롬프트로 만든다.

    긴 상담은 컨텍스트를 넘기므로 자른다. 앞뒤를 남기고 가운데를 버리는 이유:
    상담은 앞에서 용건이 나오고 뒤에서 결론이 나며, 가운데는 확인·반복 대화가
    많다. 앞만 자르면 결론을, 뒤만 자르면 용건을 잃는다.
    """
    text = transcript.strip()
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{text[:head]}\n\n...(중략)...\n\n{text[-tail:]}"
