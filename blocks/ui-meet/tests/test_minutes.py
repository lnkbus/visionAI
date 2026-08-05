"""회의록 화면 계약 테스트.

핵심: **화자 라벨이 자막보다 늦게 와도 화면이 성립한다.** 라벨을 기다렸다가
자막을 그리면 회의 자막이 통째로 늦어지므로, 두 갈래를 한 소켓으로 합치되
순서에는 의존하지 않는다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import FilterResult, SpeakerLabel
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import ActionItem, MeetingSummary, Summary, SummaryStatus
from vai_contracts.topics import Topic
from vai_contracts.ws import AgentAssistUpdate
from vai_ui_meet.app import create_app

SESSION = "sess_meet_0001"
TENANT = "t1"


def summary_stub(summary: Summary | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if summary is None:
            return httpx.Response(404, json={"detail": "없다"})
        return httpx.Response(200, json=summary.model_dump(mode="json"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sum")


def diarization_stub(speakers: list[dict] | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if speakers is None:
            return httpx.Response(404, json={"detail": "없다"})
        return httpx.Response(200, json=speakers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://dia")


def ready_minutes() -> Summary:
    return Summary(
        session_id=SESSION,
        tenant_id=TENANT,
        profile=SessionProfile.MEETING,
        status=SummaryStatus.READY,
        meeting=MeetingSummary(
            title="주간 운영회의",
            agenda=["예산 집행", "인력 계획"],
            decisions=["예산 이월 승인"],
            action_items=[ActionItem(text="집행 내역 정리", owner="김철", due="금요일")],
        ),
        model="qwen-14b",
        prompt_version="meeting-v1",
    )


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


def make_client(
    bus: InMemoryEventBus,
    *,
    summary: Summary | None = None,
    speakers: list[dict] | None = None,
) -> TestClient:
    app = create_app(
        bus=bus,
        summary_client=summary_stub(summary),
        diarization_client=diarization_stub(speakers),
    )
    return TestClient(app)


# ── 관찰 소켓 ────────────────────────────────────────────────────────────────


def test_stream_requires_a_session_id(bus: InMemoryEventBus) -> None:
    with make_client(bus) as client, pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect("/v1/meet/stream"):
            pass


def test_captions_carry_the_join_key(bus: InMemoryEventBus) -> None:
    """start_ms가 없으면 화면이 자막에 화자를 붙일 방법이 없다."""
    result = FilterResult(
        session_id=SESSION,
        tenant_id=TENANT,
        channel=ChannelRole.PARTICIPANT,
        clean_text="예산 집행 현황부터 보겠습니다",
        start_ms=4200,
        duration_ms=1800,
    )

    with make_client(bus) as client:
        with client.websocket_connect(f"/v1/meet/stream?session_id={SESSION}") as ws:

            async def push() -> None:
                await bus.publish_ui(SESSION, AgentAssistUpdate.from_filter(result))

            asyncio.run(push())
            message = ws.receive_json()

    assert message["stt_result"]["text"] == "예산 집행 현황부터 보겠습니다"
    assert message["stt_result"]["start_ms"] == 4200


def test_speaker_labels_reach_the_screen(bus: InMemoryEventBus) -> None:
    """자막과 화자 라벨은 경로가 다르다. 한 소켓으로 합쳐 화면이 한 곳만 보게 한다."""
    label = SpeakerLabel(
        session_id=SESSION,
        tenant_id=TENANT,
        channel=ChannelRole.PARTICIPANT,
        speaker_id="speaker_2",
        start_ms=4200,
        duration_ms=1800,
        confidence=0.86,
        is_new_speaker=True,
    )

    with make_client(bus) as client:
        with client.websocket_connect(f"/v1/meet/stream?session_id={SESSION}") as ws:

            async def push() -> None:
                # 화면이 구독 그룹을 등록할 틈을 준다.
                await asyncio.sleep(0.2)
                await bus.publish(Topic.SPEAKER_LABEL, label)

            asyncio.run(push())
            message = ws.receive_json()

    assert message["event"] == "speaker_label"
    assert message["speaker_id"] == "speaker_2"
    assert message["start_ms"] == 4200, "자막과 맞출 결합 키가 함께 와야 한다"
    assert message["is_new_speaker"] is True


# ── 회의록 ───────────────────────────────────────────────────────────────────


def test_minutes_are_relayed(bus: InMemoryEventBus) -> None:
    with make_client(bus, summary=ready_minutes()) as client:
        body = client.get(f"/v1/meet/summary/{SESSION}").json()

    assert body["meeting"]["title"] == "주간 운영회의"
    assert body["meeting"]["action_items"][0]["owner"] == "김철"
    assert body["prompt_version"] == "meeting-v1"


def test_missing_minutes_is_404_so_the_screen_can_retry(bus: InMemoryEventBus) -> None:
    with make_client(bus, summary=None) as client:
        assert client.get(f"/v1/meet/summary/{SESSION}").status_code == 404


# ── 참석자 ───────────────────────────────────────────────────────────────────


def test_speakers_are_listed_with_talk_time(bus: InMemoryEventBus) -> None:
    roster = [
        {"speaker_id": "speaker_1", "segments": 12, "total_ms": 90_000},
        {"speaker_id": "speaker_2", "segments": 4, "total_ms": 20_000},
    ]

    with make_client(bus, speakers=roster) as client:
        body = client.get(f"/v1/meet/speakers/{SESSION}").json()

    assert [item["speaker_id"] for item in body] == ["speaker_1", "speaker_2"]
    assert body[0]["display_name"] == ""


def test_naming_speakers_is_a_human_decision(bus: InMemoryEventBus) -> None:
    """화자분리는 "다른 사람"까지만 안다. 자동 추정하면 엉뚱한 이름이 박힌다."""
    roster = [{"speaker_id": "speaker_1", "segments": 3, "total_ms": 10_000}]

    with make_client(bus, speakers=roster) as client:
        client.put(
            f"/v1/meet/speakers/{SESSION}",
            json={
                "session_id": SESSION,
                "names": [{"speaker_id": "speaker_1", "display_name": "김철 팀장"}],
            },
        )
        body = client.get(f"/v1/meet/speakers/{SESSION}").json()

    assert body[0]["display_name"] == "김철 팀장"


def test_diarization_outage_does_not_break_the_screen(bus: InMemoryEventBus) -> None:
    """화자분리가 꺼져 있어도 회의 자막과 회의록은 계속 보여야 한다."""
    with make_client(bus, speakers=None) as client:
        response = client.get(f"/v1/meet/speakers/{SESSION}")

    assert response.status_code == 200
    assert response.json() == []


def test_minutes_page_is_served(bus: InMemoryEventBus) -> None:
    with make_client(bus) as client:
        response = client.get("/minutes")

    assert response.status_code == 200
    assert "스마트 회의록" in response.text


# ── 참석자 실명 (사람이 입력한 데이터) ───────────────────────────────────────


async def test_speaker_names_survive_a_restart() -> None:
    """화자분리는 "다른 사람"까지만 안다. 실명은 사람이 직접 입력한 사실이다.

    프로세스 메모리에 두면 재기동으로 사라지고, 복제본이 둘이면 입력한 화면과
    조회하는 화면이 갈려 애초에 안 보인다 — 회의록의 "누가 말했는가"가
    speaker_1로 되돌아간다.
    """
    import redis.asyncio as aioredis

    from vai_ui_meet.app import RedisSpeakerNames

    client = aioredis.Redis(db=13)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis가 없다")

    try:
        await client.flushdb()
        await RedisSpeakerNames(client).put("m1", {"speaker_1": "김부장", "speaker_2": "이대리"})

        restarted = RedisSpeakerNames(client)  # 블록 재기동

        assert await restarted.get("m1") == {"speaker_1": "김부장", "speaker_2": "이대리"}
    finally:
        await client.flushdb()
        await client.aclose()


async def test_renaming_drops_removed_speakers() -> None:
    """지운 이름이 남아 있으면 회의록에 없는 참석자가 계속 붙는다."""
    from vai_ui_meet.app import InMemorySpeakerNames

    names = InMemorySpeakerNames()
    await names.put("m1", {"speaker_1": "김부장", "speaker_2": "이대리"})

    await names.put("m1", {"speaker_1": "김상무"})

    assert await names.get("m1") == {"speaker_1": "김상무"}


async def test_unknown_session_has_no_names() -> None:
    from vai_ui_meet.app import InMemorySpeakerNames

    assert await InMemorySpeakerNames().get("없는회의") == {}
