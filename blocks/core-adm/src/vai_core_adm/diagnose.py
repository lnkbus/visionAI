"""블록 인터페이스 시험 — "이 블록이 실제로 무엇을 돌려주는가".

`/readyz` 는 프로세스가 살아 있다는 것만 말한다. 살아 있는데 아무것도 못 하는
상태는 감사에서 반복해서 나왔다 — 희소 색인이 빈 채로 검색이 계속 응답하고,
룰을 고쳐도 워커가 다시 읽지 않고, 잡음 필터가 아무것도 거르지 않았다.
전부 `/readyz` 는 초록이었다.

그래서 **입력을 넣고 출력을 본다.** 두 가지 방식을 쓴다:

* **왕복(round-trip)** — 입력 토픽에 이벤트를 쓰고 출력 토픽에서 같은
  ``session_id`` 를 기다린다. STT·VAD·화자분리·요약처럼 HTTP가 없는
  이벤트 전용 워커를 시험하는 유일한 길이며, **실제 운영 경로를 그대로
  통과한다** — 시험용 우회로를 따로 만들면 "시험은 되는데 실제는 안 된다"가
  가능해진다.
* **직접 호출** — 동기 API가 있는 블록(TTS 미리듣기, 룰 시험, 검색)은
  그 API를 그대로 부른다. 실패 지점이 블록 하나로 좁혀진다.

**주의: 왕복 시험은 진짜 이벤트를 흘린다.** 시험 이벤트도 뒤따르는 블록들이
소비한다(예: ``audio.segment`` 은 STT와 화자분리가 함께 받는다). 그래서
세션 ID에 :data:`DIAG_PREFIX` 를 붙여 운영 데이터와 구분할 수 있게 하고,
``session.closed`` 는 **절대 발행하지 않는다** — 그것 하나로 요약 배치가
돌아 시험이 회의록을 만든다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from vai_common.bus import EventBus
from vai_contracts.events import (
    AudioChunk,
    AudioSegment,
    BaseEvent,
    FilterResult,
    SttDelta,
)
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.topics import Topic

DIAG_PREFIX = "diag-"
"""시험 세션 표시. 운영 세션과 섞이지 않게 하는 유일한 표식이다."""

DEFAULT_TIMEOUT_S = 8.0
"""왕복 상한. 넘으면 '느리다'가 아니라 '응답이 없다'로 본다 —
사람이 화면 앞에서 기다릴 수 있는 시간이 그 정도다."""

SAMPLE_RATE = 16000
SAMPLE_TEXT = "카드 재발급 신청하려고 하는데 수수료가 얼마인가요"


def diag_session() -> str:
    return f"{DIAG_PREFIX}{uuid.uuid4().hex[:12]}"


def _tone(duration_ms: int) -> bytes:
    """말소리처럼 에너지가 있는 PCM. 무음을 넣으면 VAD가 정상적으로 버린다 —
    그리고 그 정상 동작이 '응답 없음'으로 보인다."""
    import math
    import struct

    count = int(SAMPLE_RATE * duration_ms / 1000)
    return struct.pack(
        f"<{count}h",
        *(int(12000 * math.sin(2 * math.pi * 220 * n / SAMPLE_RATE)) for n in range(count)),
    )


@dataclass(frozen=True)
class RoundTrip:
    """블록 하나의 왕복 시험 정의. 토픽은 카탈로그의 계약 그대로다."""

    block_id: str
    inbound: Topic
    outbound: Topic
    label: str
    build: Any  # (session_id, tenant_id, text) -> BaseEvent
    model: type[BaseEvent]
    note: str = ""


def _audio_chunk(session_id: str, tenant_id: str, _text: str) -> AudioChunk:
    return AudioChunk(
        session_id=session_id,
        tenant_id=tenant_id,
        channel=ChannelRole.CUSTOMER,
        sample_rate=SAMPLE_RATE,
        pcm=_tone(700),
        profile=SessionProfile.AICC,
    )


def _audio_segment(session_id: str, tenant_id: str, _text: str) -> AudioSegment:
    return AudioSegment(
        session_id=session_id,
        tenant_id=tenant_id,
        channel=ChannelRole.CUSTOMER,
        sample_rate=SAMPLE_RATE,
        pcm=_tone(700),
        profile=SessionProfile.AICC,
        start_ms=0,
        duration_ms=700,
    )


def _stt_delta(session_id: str, tenant_id: str, text: str) -> SttDelta:
    return SttDelta(
        session_id=session_id,
        tenant_id=tenant_id,
        channel=ChannelRole.CUSTOMER,
        text=text,
        is_final=True,
        confidence=0.95,
        duration_ms=700,
    )


def _filter_clean(session_id: str, tenant_id: str, text: str) -> FilterResult:
    return FilterResult(
        session_id=session_id,
        tenant_id=tenant_id,
        channel=ChannelRole.CUSTOMER,
        clean_text=text,
        is_final=True,
        duration_ms=700,
    )


ROUND_TRIPS: dict[str, RoundTrip] = {
    trip.block_id: trip
    for trip in (
        RoundTrip(
            "AUD-VAD",
            Topic.AUDIO_IN,
            Topic.AUDIO_SEGMENT,
            "오디오 청크 → 발화 구간",
            _audio_chunk,
            AudioSegment,
            note="무음이 아니라 톤을 넣는다. 무음은 정상적으로 버려지고, "
            "그 정상 동작이 화면에서는 '응답 없음'과 구분되지 않는다.",
        ),
        RoundTrip(
            "STT-CORE",
            Topic.AUDIO_SEGMENT,
            Topic.STT_DELTA,
            "발화 구간 → 인식 결과",
            _audio_segment,
            SttDelta,
            note="fake 어댑터에서는 고정 문구가 나온다. 실제 모델 품질은 "
            "여기서 알 수 없고 오디오 평가셋이 필요하다.",
        ),
        RoundTrip(
            "SPK-DIA",
            Topic.AUDIO_SEGMENT,
            Topic.SPEAKER_LABEL,
            "발화 구간 → 화자 라벨",
            _audio_segment,
            BaseEvent,
            note="한 구간만으로는 화자 구분이 성립하지 않는다. 여기서 보는 것은 "
            "'라벨이 붙는가'이지 '정확한가'가 아니다.",
        ),
        RoundTrip(
            "FLT-MICRO",
            Topic.STT_DELTA,
            Topic.FILTER_CLEAN,
            "인식 결과 → 마스킹·룰 적용",
            _stt_delta,
            FilterResult,
        ),
        RoundTrip(
            "TA-ASSIST",
            Topic.FILTER_CLEAN,
            Topic.ASSIST_POPUP,
            "마스킹본 → 지식 팝업",
            _filter_clean,
            BaseEvent,
            note="근거를 못 찾으면 팝업을 내지 않는 것이 정상이다 — "
            "지식베이스가 비어 있으면 '응답 없음'으로 보인다.",
        ),
        RoundTrip(
            "BOT-VOICE",
            Topic.FILTER_CLEAN,
            Topic.TTS_REQUEST,
            "마스킹본 → 합성 요청",
            _filter_clean,
            BaseEvent,
            note="시나리오가 배포돼 있어야 진행된다.",
        ),
    )
}


class ProbeResult(BaseModel):
    block_id: str
    kind: str
    label: str
    ok: bool
    latency_ms: int
    detail: str = ""
    payload: dict[str, Any] = {}
    note: str = ""
    session_id: str = ""


@dataclass
class _Collector:
    """출력 토픽에서 우리 세션 것만 골라 받는다.

    임시 컨슈머 그룹을 쓴다. 운영 그룹에 끼어들면 **시험이 운영 메시지를
    가로챈다** — 진단이 장애를 만드는 가장 빠른 길이다.
    """

    bus: EventBus
    topic: Topic
    model: type[BaseEvent]
    session_id: str
    group: str = field(default_factory=lambda: f"diag:{uuid.uuid4().hex[:8]}")

    async def start(self) -> None:
        """발행 **전에** 그룹을 만들어 둔다.

        비동기 제너레이터는 첫 anext 전까지 아무것도 하지 않으므로, 구독
        코루틴을 먼저 만들어 두는 것만으로는 부족하다. 순서가 뒤집히면 빠른
        블록의 응답을 놓치고 '응답 없음'이 된다 — 잘 도는 블록일수록 그렇다.
        """
        await self.bus.prepare_group(self.topic, self.group, from_now=True)

    async def wait(self, timeout_s: float) -> BaseEvent | None:
        deadline = time.monotonic() + timeout_s
        stream = self.bus.consume(self.topic, self.model, group=self.group, consumer=self.group)
        try:
            while time.monotonic() < deadline:
                try:
                    delivery = await asyncio.wait_for(
                        anext(stream), timeout=max(0.1, deadline - time.monotonic())
                    )
                except (TimeoutError, StopAsyncIteration):
                    return None
                await delivery.ack()
                if delivery.event.session_id == self.session_id:
                    return delivery.event
            return None
        finally:
            await stream.aclose()


async def run_round_trip(
    bus: EventBus,
    trip: RoundTrip,
    *,
    tenant_id: str,
    text: str = SAMPLE_TEXT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """입력을 흘리고 출력이 돌아오는지 본다."""
    session_id = diag_session()
    collector = _Collector(bus, trip.outbound, trip.model, session_id)

    # 그룹 등록 → 대기 시작 → 발행. 순서가 이것이어야 응답을 놓치지 않는다.
    await collector.start()
    started = time.monotonic()
    waiter = asyncio.create_task(collector.wait(timeout_s))
    await asyncio.sleep(0)
    await bus.publish(trip.inbound, trip.build(session_id, tenant_id, text))

    event = await waiter
    latency_ms = int((time.monotonic() - started) * 1000)

    if event is None:
        return ProbeResult(
            block_id=trip.block_id,
            kind="round_trip",
            label=trip.label,
            ok=False,
            latency_ms=latency_ms,
            detail=(
                f"{timeout_s:.0f}초 안에 '{trip.outbound.value}' 응답이 없다 — "
                f"블록이 안 떴거나, '{trip.inbound.value}' 을 소비하지 않거나, "
                "처리 중 예외로 죽었다. `docker compose logs "
                f"{trip.block_id.lower()}` 를 본다"
            ),
            note=trip.note,
            session_id=session_id,
        )

    return ProbeResult(
        block_id=trip.block_id,
        kind="round_trip",
        label=trip.label,
        ok=True,
        latency_ms=latency_ms,
        detail=f"{trip.inbound.value} → {trip.outbound.value}",
        payload=_summarize(event),
        note=trip.note,
        session_id=session_id,
    )


MAX_TEXT = 200
"""화면에 올릴 문자열 상한. 오디오 PCM이 그대로 오면 화면이 멎는다."""


def _summarize(event: BaseEvent) -> dict[str, Any]:
    """화면에 보여 줄 만큼만. bytes 필드는 길이로 바꾼다."""
    out: dict[str, Any] = {}
    for name, value in event.model_dump(mode="json").items():
        if name in {"schema_version", "emitted_at", "tenant_id"}:
            continue
        if isinstance(value, str) and len(value) > MAX_TEXT:
            out[name] = f"({len(value)}자)"
        else:
            out[name] = value
    raw = getattr(event, "pcm", None)
    if isinstance(raw, bytes):
        out["pcm"] = f"({len(raw)}바이트)"
    return out


# ── 직접 호출 ───────────────────────────────────────────────────────────────
#
# 동기 API가 있는 블록은 그 API를 그대로 부른다. 왕복보다 실패 지점이
# 좁고(버스가 아니라 그 블록), 버스가 죽어 있어도 시험할 수 있다.


@dataclass(frozen=True)
class DirectCall:
    block_id: str
    label: str
    endpoint_key: str
    path: str
    body: dict[str, Any]
    note: str = ""


DIRECT_CALLS: dict[str, DirectCall] = {
    call.block_id: call
    for call in (
        DirectCall(
            "TTS-CORE",
            "문장 → 읽기 텍스트 미리보기",
            "tts",
            "/internal/v1/tts/preview",
            {"text": "재발급 수수료는 2,000원이며 3영업일 걸립니다"},
            note="숫자·금액·단위 읽기를 확인하는 자리다. 소리는 합성 경로에서 나온다.",
        ),
        DirectCall(
            "FLT-MICRO",
            "개인정보 마스킹 시험",
            "filter",
            "/internal/v1/filter/test",
            {"text": "제 번호는 010-1234-5678이고 카드번호는 1234-5678-9012-3456입니다"},
            note="번호가 그대로 보이면 마스킹이 뚫린 것이다 — 조사가 붙은 형태로도 넣어 본다.",
        ),
        DirectCall(
            "RAG-SRCH",
            "지식 검색",
            "search",
            "/internal/v1/search",
            {"query": SAMPLE_TEXT, "top_k": 3},
            note="희소 점수가 전부 0이면 색인이 비어 있다 — 검색은 응답하지만 절반만 동작한다.",
        ),
        DirectCall(
            "LLM-GW",
            "모델 응답",
            "llm",
            "/internal/v1/complete",
            {"prompt": "한 문장으로 인사해 줘", "max_tokens": 64},
            note="어댑터가 echo면 프롬프트가 그대로 돌아온다 — 모델이 붙지 않은 상태다.",
        ),
    )
}


async def run_direct_call(
    client: Any,
    call: DirectCall,
    base_url: str,
    *,
    tenant_id: str,
    body: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    payload = {"tenant_id": tenant_id, **call.body, **(body or {})}
    started = time.monotonic()
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}{call.path}", json=payload, timeout=timeout_s
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # httpx 계열 전부. 화면은 이유를 그대로 보여 준다.
        return ProbeResult(
            block_id=call.block_id,
            kind="direct",
            label=call.label,
            ok=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=f"{call.path} 호출 실패: {exc}",
            note=call.note,
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    trimmed = {
        key: (f"({len(value)}자)" if isinstance(value, str) and len(value) > MAX_TEXT else value)
        for key, value in (data if isinstance(data, dict) else {"result": data}).items()
    }
    return ProbeResult(
        block_id=call.block_id,
        kind="direct",
        label=call.label,
        ok=True,
        latency_ms=latency_ms,
        detail=call.path,
        payload=trimmed,
        note=call.note,
    )


def probes() -> list[dict[str, Any]]:
    """화면이 그릴 시험 목록. 카탈로그가 아니라 여기가 기준이다 —
    시험이 정의되지 않은 블록을 '시험 통과'로 보이게 하지 않는다."""
    items: list[dict[str, Any]] = []
    for trip in ROUND_TRIPS.values():
        items.append(
            {
                "block_id": trip.block_id,
                "kind": "round_trip",
                "label": trip.label,
                "inbound": trip.inbound.value,
                "outbound": trip.outbound.value,
                "note": trip.note,
            }
        )
    for call in DIRECT_CALLS.values():
        items.append(
            {
                "block_id": call.block_id,
                "kind": "direct",
                "label": call.label,
                "path": call.path,
                "note": call.note,
            }
        )
    return sorted(items, key=lambda item: (item["block_id"], item["kind"]))
