"""CORE-BUS 세션 레지스트리 계약 테스트."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import SessionClosed
from vai_contracts.session import SessionProfile, SessionState
from vai_contracts.topics import Topic
from vai_core_bus.app import create_app
from vai_core_bus.store import InMemorySessionStore


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    # 종료 이벤트를 관찰하려면 발행 전에 그룹이 등록돼 있어야 한다.
    bus.register_group(Topic.SESSION_CLOSED, "test")
    return bus


@pytest.fixture
async def client(bus: InMemoryEventBus):
    app = create_app(bus=bus, store=InMemorySessionStore())
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://bus") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def create_session(client: httpx.AsyncClient, **overrides: object) -> dict:
    payload = {"tenant_id": "t1", "profile": "aicc"} | overrides
    response = await client.post("/internal/v1/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_returns_session_with_generated_id(client: httpx.AsyncClient) -> None:
    session = await create_session(client)

    assert session["session_id"].startswith("sess_")
    assert session["profile"] == SessionProfile.AICC.value
    assert session["state"] == SessionState.ACTIVE.value


async def test_get_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/internal/v1/sessions/sess_nope")

    assert response.status_code == 404


async def test_close_publishes_summary_trigger(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    session = await create_session(client, profile="meeting")

    response = await client.post(f"/internal/v1/sessions/{session['session_id']}/close")

    assert response.status_code == 200
    assert response.json()["state"] == SessionState.CLOSED.value

    queue = bus._queues[(Topic.SESSION_CLOSED, "test")]
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert isinstance(event, SessionClosed)
    assert event.session_id == session["session_id"]
    assert event.profile is SessionProfile.MEETING


async def test_double_close_publishes_once(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    """요약 배치가 두 번 돌면 GPU 낭비이자 중복 산출물이 된다."""
    session = await create_session(client)
    path = f"/internal/v1/sessions/{session['session_id']}/close"

    await client.post(path)
    second = await client.post(path)

    assert second.status_code == 200
    assert bus._queues[(Topic.SESSION_CLOSED, "test")].qsize() == 1


async def test_healthz_reports_block_identity(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["block"] == "CORE-BUS"
