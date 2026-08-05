"""상담원 워크스페이스 계약 테스트.

이 블록의 두 가지 책임을 확인한다:
① 세션을 **관찰만** 한다 — 화면이 죽어도 상담은 계속되어야 한다
② 팝업 채택 피드백을 수집한다 — 품질 개선 루프의 유일한 수집 지점이다
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AssistPopup, KnowledgeHit
from vai_contracts.session import SessionProfile
from vai_contracts.summary import AiccSummary, Summary, SummaryStatus
from vai_contracts.ws import AgentAssistUpdate
from vai_ui_agent.app import create_app

SESSION = "sess_ws_0001"
TENANT = "demo"


def studio_stub(fail: bool = False) -> tuple[httpx.AsyncClient, list[dict]]:
    """SCN-STUDIO 대역. 받은 피드백을 그대로 모아 둔다."""
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if fail:
            return httpx.Response(500, json={"detail": "down"})
        received.append(json.loads(request.content))
        return httpx.Response(202, json={"status": "accepted"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://studio"
    ), received


def summary_stub(summary: Summary | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if summary is None:
            return httpx.Response(404, json={"detail": "없다"})
        return httpx.Response(200, json=summary.model_dump(mode="json"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sum")


def ready_summary() -> Summary:
    return Summary(
        session_id=SESSION,
        tenant_id=TENANT,
        profile=SessionProfile.AICC,
        status=SummaryStatus.READY,
        aicc=AiccSummary(category="카드", customer_request="결제일 연기 요청"),
        model="qwen-14b",
        prompt_version="aicc-v1",
    )


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


def make_client(
    bus: InMemoryEventBus,
    *,
    studio: httpx.AsyncClient | None = None,
    summary: Summary | None = None,
) -> TestClient:
    app = create_app(
        bus=bus,
        studio_client=studio or studio_stub()[0],
        summary_client=summary_stub(summary),
    )
    return TestClient(app)


# ── 관찰 소켓 ────────────────────────────────────────────────────────────────


def test_stream_requires_a_session_id(bus: InMemoryEventBus) -> None:
    with make_client(bus) as client, pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect("/v1/agent/stream"):
            pass


def test_workspace_receives_captions_and_popups(bus: InMemoryEventBus) -> None:
    """화면이 실제로 자막과 팝업을 받는지 — 이 경로가 상담원이 보는 전부다."""
    popup = AssistPopup(
        session_id=SESSION,
        tenant_id=TENANT,
        query="결제일 연기",
        hits=[
            KnowledgeHit(
                doc_id="doc_card_policy_012",
                title="결제일 변경 규정",
                score=0.92,
                snippet="최대 5일까지 신청 가능",
            )
        ],
    )

    with make_client(bus) as client:
        with client.websocket_connect(f"/v1/agent/stream?session_id={SESSION}") as ws:

            async def push() -> None:
                await bus.publish_ui(SESSION, AgentAssistUpdate.from_popup(popup))

            asyncio.run(push())
            message = ws.receive_json()

    assert message["event"] == "agent_assist_update"
    assert message["knowledge_popup"][0]["doc_id"] == "doc_card_policy_012"


def test_workspace_does_not_receive_other_sessions(bus: InMemoryEventBus) -> None:
    """상담원 화면이 남의 상담을 받으면 그 자체가 사고다."""
    received: list[dict] = []

    async def watch() -> None:
        async for payload in bus.subscribe_ui("other-session"):
            received.append(payload)

    with make_client(bus) as client:
        with client.websocket_connect(f"/v1/agent/stream?session_id={SESSION}"):

            async def push() -> None:
                task = asyncio.create_task(watch())
                await asyncio.sleep(0)
                await bus.publish_ui("other-session", AgentAssistUpdate(session_id="other-session"))
                await asyncio.sleep(0.05)
                task.cancel()

            asyncio.run(push())

    assert len(received) == 1, "다른 세션 이벤트는 그 세션 구독자에게만 간다"


# ── 피드백 ───────────────────────────────────────────────────────────────────


def test_feedback_reaches_the_studio(bus: InMemoryEventBus) -> None:
    """피드백 버튼이 없으면 SCN-STUDIO의 채택률 화면은 영원히 비어 있다."""
    studio, received = studio_stub()

    with make_client(bus, studio=studio) as client:
        response = client.post(
            "/v1/agent/feedback",
            json={
                "session_id": SESSION,
                "tenant_id": TENANT,
                "doc_id": "doc_1",
                "query": "결제일 연기",
                "accepted": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    assert received[0]["accepted"] is True
    assert received[0]["query"] == "결제일 연기"


def test_feedback_failure_is_not_shown_to_the_agent(bus: InMemoryEventBus) -> None:
    """수집 실패로 에러 모달이 뜨면 상담원은 다음부터 버튼을 누르지 않는다."""
    studio, _ = studio_stub(fail=True)

    with make_client(bus, studio=studio) as client:
        response = client.post(
            "/v1/agent/feedback",
            json={
                "session_id": SESSION,
                "tenant_id": TENANT,
                "doc_id": "doc_1",
                "query": "q",
                "accepted": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "deferred"


# ── 요약 ─────────────────────────────────────────────────────────────────────


def test_summary_is_relayed(bus: InMemoryEventBus) -> None:
    with make_client(bus, summary=ready_summary()) as client:
        body = client.get(f"/v1/agent/summary/{SESSION}").json()

    assert body["status"] == "ready"
    assert body["aicc"]["category"] == "카드"
    assert body["prompt_version"] == "aicc-v1", "감사 추적을 위해 프롬프트 버전이 화면까지 온다"


def test_missing_summary_is_404_so_the_screen_can_retry(bus: InMemoryEventBus) -> None:
    with make_client(bus, summary=None) as client:
        assert client.get(f"/v1/agent/summary/{SESSION}").status_code == 404


def test_workspace_page_is_served(bus: InMemoryEventBus) -> None:
    with make_client(bus) as client:
        response = client.get("/workspace")

    assert response.status_code == 200
    assert "상담원 워크스페이스" in response.text
