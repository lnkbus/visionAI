"""CORE-BUS 세션 레지스트리 계약 테스트."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from vai_common.bus import InMemoryEventBus
from vai_common.license import BlockGrant, LicenseGate
from vai_contracts.events import SessionClosed
from vai_contracts.session import SessionProfile, SessionState
from vai_contracts.topics import Topic
from vai_core_bus.app import create_app
from vai_core_bus.store import InMemorySessionStore, SessionStore


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


# --- 라이선스 동시 채널 한도 ---------------------------------------------
#
# 여기서 세지 않으면 .lic의 채널 수는 장식이고, "산 만큼만 쓴다"는 계약을
# 지킬 수단이 없다. 반대로 잘못 세면 정상 상담이 거부된다 — 둘 다 계약 사고다.


def _gated_app(limit: int, block_id: str = "TA-ASSIST"):
    from vai_common.license import BlockGrant, LicenseGate

    bus = InMemoryEventBus()
    store = InMemorySessionStore()
    app = create_app(bus=bus, store=store)
    app.state.license = LicenseGate(
        {
            block_id: BlockGrant(True, {"concurrent_channels": limit}),
            "STT-CORE": BlockGrant(True, {"concurrent_channels": limit * 10}),
        },
        None,
        dev_mode=False,
    )
    return app, bus, store


async def _open(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/internal/v1/sessions", json={"tenant_id": "acme", "profile": "aicc"})


async def test_한도까지는_세션이_열린다() -> None:
    app, _, _ = _gated_app(2)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        assert (await _open(client)).status_code == 201
        assert (await _open(client)).status_code == 201


async def test_한도를_넘으면_429로_거부한다() -> None:
    app, _, _ = _gated_app(2)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        await _open(client)
        await _open(client)
        denied = await _open(client)

    assert denied.status_code == 429
    # 어느 블록이 상한을 정했는지 알려 줘야 증설 견적으로 이어진다.
    assert "TA-ASSIST" in denied.json()["detail"]


async def test_가장_빡빡한_블록이_상한을_정한다() -> None:
    """TA-ASSIST를 50채널만 샀는데 STT를 100채널 샀다고 100건을 받으면
    51번째부터 팝업 없는 상담이 된다. 계약과 동작이 어긋나는 쪽이 훨씬 나쁘다."""
    app, _, _ = _gated_app(1, block_id="FLT-MICRO")
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        await _open(client)
        denied = await _open(client)
    assert denied.status_code == 429
    assert "FLT-MICRO" in denied.json()["detail"]


async def test_세션을_닫으면_채널이_돌아온다() -> None:
    """닫힌 세션이 계속 채널을 잡고 있으면 한도가 조금씩 새어 나가고,
    결국 라이선스 안에서 상담을 못 받는 상태가 된다."""
    app, _, _ = _gated_app(1)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        first = (await _open(client)).json()
        assert (await _open(client)).status_code == 429

        await client.post(f"/internal/v1/sessions/{first['session_id']}/close")
        assert (await _open(client)).status_code == 201


async def test_거부가_감사에_남는다() -> None:
    """거부는 로그가 아니라 감사에 남아야 한다. 운영자가 '왜 통화가 안 받아졌나'를
    물었을 때 답이 있어야 한다."""
    app, bus, _ = _gated_app(1)
    bus.register_group(Topic.AUDIT_LOG, "probe")
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        await _open(client)
        await _open(client)

    queue = bus._queues[(Topic.AUDIT_LOG, "probe")]
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    denied = [e for e in events if e.outcome == "denied"]
    assert len(denied) == 1
    assert denied[0].detail["limit"] == "1"
    assert denied[0].detail["limiting_block"] == "TA-ASSIST"


async def test_개발_모드에서는_한도가_없다() -> None:
    """라이선스 없이 도는 개발·데모 구성에서 채널 한도로 막히면 아무것도 못 한다."""
    app = create_app(bus=InMemoryEventBus(), store=InMemorySessionStore())
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        for _ in range(5):
            assert (await _open(client)).status_code == 201
        capacity = (await client.get("/internal/v1/capacity")).json()
    assert capacity["limit"] is None
    assert capacity["active"] == 5


async def test_사용률을_보고한다() -> None:
    """상한에 부딪힌 뒤에 아는 것은 이미 상담을 놓친 뒤다."""
    app, _, _ = _gated_app(4)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bus") as client,
        app.router.lifespan_context(app),
    ):
        await _open(client)
        await _open(client)
        capacity = (await client.get("/internal/v1/capacity")).json()

    assert capacity["active"] == 2 and capacity["limit"] == 4
    assert capacity["utilization"] == 0.5
    assert capacity["limiting_block"] == "TA-ASSIST"


# --- Redis 저장소의 활성 집계 --------------------------------------------


@pytest.fixture
async def redis_store():
    """실제 Redis가 있을 때만 돈다. 정렬 집합 연산은 인메모리 구현으로 대체할 수 없다."""
    import os

    from redis.asyncio import Redis

    url = os.environ.get("VAI_TEST_REDIS_URL", "redis://localhost:6379/15")
    redis = Redis.from_url(url)
    try:
        await redis.ping()
    except Exception:
        pytest.skip("Redis가 없다")
    await redis.flushdb()
    from vai_core_bus.store import RedisSessionStore

    yield RedisSessionStore(redis)
    await redis.flushdb()
    await redis.aclose()


def _session(sid: str, **kw):
    from vai_contracts.session import Session

    return Session(session_id=sid, tenant_id="acme", profile=SessionProfile.AICC, **kw)


async def test_Redis_활성_수를_센다(redis_store) -> None:
    await redis_store.save(_session("s1"))
    await redis_store.save(_session("s2"))
    assert await redis_store.active_count() == 2

    await redis_store.close("s1")
    assert await redis_store.active_count() == 1


async def test_같은_세션을_두_번_저장해도_한_번만_센다(redis_store) -> None:
    """상태 갱신(예: 메타데이터 변경)마다 채널이 늘면 한도가 금방 소진된다."""
    session = _session("s1")
    await redis_store.save(session)
    await redis_store.save(session)
    assert await redis_store.active_count() == 1


async def test_TTL을_넘긴_세션은_걷어낸다(redis_store) -> None:
    """종료 이벤트를 놓친 세션이 집합에 영원히 남으면 채널이 조금씩 새어 나가고,
    결국 라이선스 안에서 상담을 못 받는 상태가 된다 — 원인을 찾기 가장 어렵다."""
    from datetime import UTC, datetime, timedelta

    from vai_core_bus.store import TTL_SECONDS

    stale = _session("old", created_at=datetime.now(UTC) - timedelta(seconds=TTL_SECONDS + 60))
    await redis_store.save(stale)
    await redis_store.save(_session("fresh"))

    assert await redis_store.active_count() == 1


# ── 동시 인입에서의 한도 ─────────────────────────────────────────────────────


def _limited_app(store: SessionStore, limit: int) -> FastAPI:
    app = create_app(store=store, bus=InMemoryEventBus())
    app.state.store = store
    app.state.bus = InMemoryEventBus()
    app.state.license = LicenseGate(
        {"CORE-BUS": BlockGrant(True, {"concurrent_channels": limit})}, None, dev_mode=False
    )
    return app


async def _burst(app: FastAPI, count: int) -> list[int]:
    body = {"tenant_id": "t1", "profile": "aicc"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://core-bus"
    ) as client:
        responses = await asyncio.gather(
            *[client.post("/internal/v1/sessions", json=body) for _ in range(count)]
        )
    return [response.status_code for response in responses]


async def test_burst_arrival_cannot_exceed_the_licensed_limit() -> None:
    """착신이 몰릴 때만 뚫리는 한도는 한도가 아니다.

    세고 나서 따로 저장하면 그 사이에 다른 요청이 끼어든다. 콜센터의 인입이
    정확히 그런 모양이라, 재현이 어려운 채로 라이선스가 장식이 된다.
    """
    store = InMemorySessionStore()

    codes = await _burst(_limited_app(store, limit=2), count=8)

    assert await store.active_count() == 2, f"한도를 넘겼다: {codes}"
    assert codes.count(201) == 2
    assert codes.count(429) == 6


async def test_closing_a_session_frees_a_channel() -> None:
    """한도가 지켜지되 돌아오지 않으면 그건 그것대로 장애다."""
    store = InMemorySessionStore()
    app = _limited_app(store, limit=1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://core-bus"
    ) as client:
        body = {"tenant_id": "t1", "profile": "aicc"}
        first = await client.post("/internal/v1/sessions", json=body)
        assert first.status_code == 201
        assert (await client.post("/internal/v1/sessions", json=body)).status_code == 429

        session_id = first.json()["session_id"]
        await client.post(f"/internal/v1/sessions/{session_id}/close")

        assert (await client.post("/internal/v1/sessions", json=body)).status_code == 201


async def test_no_limit_means_no_reservation_failure() -> None:
    """라이선스에 채널 한도가 없으면(개발 모드 등) 막지 않는다."""
    store = InMemorySessionStore()
    app = create_app(store=store, bus=InMemoryEventBus())
    app.state.store = store
    app.state.bus = InMemoryEventBus()
    app.state.license = LicenseGate({}, None, dev_mode=True)

    codes = await _burst(app, count=5)

    assert codes == [201] * 5
