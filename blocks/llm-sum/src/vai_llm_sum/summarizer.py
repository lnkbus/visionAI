"""요약 생성기 — 녹취록을 구조화 산출물로 바꾼다.

LLM에 JSON을 요구하지만 **JSON이 온다고 믿지 않는다.** 모델은 코드펜스를 붙이거나
설명을 덧붙이거나 필드를 빠뜨린다. 파싱 실패로 요약 전체를 버리면 상담 이력이
비므로, 최대한 건져 내되 무엇이 실패했는지는 남긴다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from vai_contracts.summary import ActionItem, AiccSummary, MeetingSummary

log = logging.getLogger(__name__)

# ```json ... ``` 코드펜스를 벗긴다. 지시해도 붙이는 모델이 많다.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json_object(raw: str) -> dict[str, Any]:
    """LLM 출력에서 JSON 객체를 건져 낸다.

    실패 시 빈 dict. 예외를 던지지 않는 이유는 호출부가 "부분 실패"를
    상태로 기록하고 계속 진행해야 하기 때문이다.
    """
    text = _FENCE.sub("", raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 설명을 덧붙인 응답에서 첫 번째 완결된 객체만 잘라 재시도한다.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            log.warning("요약 응답에서 JSON을 찾지 못했다")
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            log.warning("요약 응답 JSON 파싱 실패")
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_str_list(value: Any, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def to_aicc_summary(raw: str) -> AiccSummary:
    """AICC 표준 요약으로 변환. 누락 필드는 빈 값으로 둔다."""
    data = parse_json_object(raw)
    return AiccSummary(
        category=str(data.get("category", "")).strip(),
        subcategory=str(data.get("subcategory", "")).strip(),
        customer_request=str(data.get("customer_request", "")).strip(),
        agent_response=str(data.get("agent_response", "")).strip(),
        resolution=str(data.get("resolution", "")).strip(),
        follow_up=str(data.get("follow_up", "")).strip(),
        keywords=_as_str_list(data.get("keywords"), limit=10),
    )


def to_meeting_summary(raw: str) -> MeetingSummary:
    data = parse_json_object(raw)
    items: list[ActionItem] = []
    for entry in data.get("action_items", []) or []:
        if isinstance(entry, dict):
            text = str(entry.get("text", "")).strip()
            if text:
                items.append(
                    ActionItem(
                        text=text,
                        owner=str(entry.get("owner", "")).strip(),
                        due=str(entry.get("due", "")).strip(),
                    )
                )
        elif isinstance(entry, str) and entry.strip():
            items.append(ActionItem(text=entry.strip()))

    return MeetingSummary(
        title=str(data.get("title", "")).strip(),
        agenda=_as_str_list(data.get("agenda")),
        decisions=_as_str_list(data.get("decisions")),
        action_items=items,
        participants=_as_str_list(data.get("participants")),
    )


def is_empty(summary: AiccSummary | MeetingSummary) -> bool:
    """건진 게 아무것도 없는지. True면 실패로 기록해야 한다."""
    if isinstance(summary, AiccSummary):
        return not any(
            [summary.category, summary.customer_request, summary.agent_response, summary.resolution]
        )
    return not any([summary.title, summary.agenda, summary.decisions, summary.action_items])
