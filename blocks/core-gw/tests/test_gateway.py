"""CORE-GW 계약 테스트 — 인증, 세션 API, 사양서 §4 WebSocket 프로토콜.

CORE-BUS를 ASGI로 직접 물려 두 블록의 계약을 실제로 왕복시킨다.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AudioChunk
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.topics import Topic
from vai_core_bus.app import create_app as create_bus_app
from vai_core_bus.store import InMemorySessionStore
from vai_core_gw.app import GatewaySettings, create_app
from vai_core_gw.auth import issue_access_token, issue_session_token
from vai_core_gw.bus_client import CoreBusClient

SECRET = "test-secret-long-enough-for-hmac-sha256"
PCM = b"\x10\x00" * 160  # 16bit 정렬된 10ms 분량


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    bus.register_group(Topic.AUDIO_IN, "test")
    return bus


@pytest.fixture
def core_bus_client(bus: InMemoryEventBus) -> CoreBusClient:
    bus_app = create_bus_app(bus=bus, store=InMemorySessionStore())
    # CORE-BUS는 lifespan에서 state를 채우므로 TestClient로 기동시켜 둔다.
    started = TestClient(bus_app)
    started.__enter__()
    transport = ASGITransport(app=bus_app)
    return CoreBusClient(
        "http://bus", client=httpx.AsyncClient(transport=transport, base_url="http://bus")
    )


@pytest.fixture
def client(bus: InMemoryEventBus, core_bus_client: CoreBusClient) -> TestClient:
    app = create_app(
        bus=bus,
        core_bus=core_bus_client,
        gw_settings=GatewaySettings(jwt_secret=SECRET, dev_auth=False),
    )
    with TestClient(app) as test_client:
        yield test_client


def auth_header(tenant: str = "t1") -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_access_token(tenant, 'user-1', SECRET)}"}


def start_session(client: TestClient, tenant: str = "t1") -> dict:
    response = client.post("/v1/sessions", json={"profile": "aicc"}, headers=auth_header(tenant))
    assert response.status_code == 201, response.text
    return response.json()


def test_session_requires_authentication(client: TestClient) -> None:
    assert client.post("/v1/sessions", json={"profile": "aicc"}).status_code == 401


def test_session_start_returns_ws_token(client: TestClient) -> None:
    body = start_session(client)

    assert body["session"]["session_id"].startswith("sess_")
    assert body["ws_token"]


def test_tenant_comes_from_token_not_body(client: TestClient) -> None:
    """본문의 tenant_id를 신뢰하면 교차 테넌트 접근이 열린다."""
    response = client.post(
        "/v1/sessions",
        json={"profile": "aicc", "tenant_id": "victim-tenant"},
        headers=auth_header("attacker-tenant"),
    )

    assert response.status_code == 201
    assert response.json()["session"]["tenant_id"] == "attacker-tenant"


def test_close_hides_other_tenants_session(client: TestClient) -> None:
    """존재 여부 자체가 정보이므로 403이 아니라 404로 답해야 한다."""
    session_id = start_session(client, "t1")["session"]["session_id"]

    response = client.post(f"/v1/sessions/{session_id}/close", headers=auth_header("t2"))

    assert response.status_code == 404


def test_websocket_rejects_invalid_token(client: TestClient) -> None:
    with pytest.raises(Exception):  # noqa: B017 - starlette가 닫힌 핸드셰이크를 예외로 알린다
        with client.websocket_connect("/v1/audio/stream?token=bogus"):
            pass


def test_audio_data_becomes_audio_in_event(client: TestClient, bus: InMemoryEventBus) -> None:
    """사양서 §4 인바운드 규격 → 내부 이벤트 변환."""
    body = start_session(client)
    session_id = body["session"]["session_id"]

    with client.websocket_connect(f"/v1/audio/stream?token={body['ws_token']}") as ws:
        ws.send_json(
            {
                "event": "audio_data",
                "session_id": session_id,
                "format": "pcm_16k",
                "channel": "customer",
                "audio_base64": base64.b64encode(PCM).decode(),
            }
        )
        # 수신 루프가 이벤트를 처리할 틈을 주기 위해 잘못된 메시지로 응답을 유도한다.
        ws.send_json({"event": "nope"})
        assert ws.receive_json()["event"] == "error"

    queue = bus._queues[(Topic.AUDIO_IN, "test")]
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert isinstance(event, AudioChunk)
    assert event.session_id == session_id
    assert event.tenant_id == "t1"
    assert event.channel is ChannelRole.CUSTOMER
    assert event.sample_rate == 16000
    assert event.profile is SessionProfile.AICC
    assert event.pcm == PCM, "base64는 게이트웨이에서 한 번만 풀려야 한다"


def test_audio_for_another_session_is_rejected(client: TestClient, bus: InMemoryEventBus) -> None:
    """토큰이 가리키지 않는 세션으로 오디오를 밀어 넣을 수 없어야 한다."""
    body = start_session(client)
    other = start_session(client)["session"]["session_id"]

    with client.websocket_connect(f"/v1/audio/stream?token={body['ws_token']}") as ws:
        ws.send_json(
            {
                "event": "audio_data",
                "session_id": other,
                "format": "pcm_16k",
                "channel": "customer",
                "audio_base64": base64.b64encode(PCM).decode(),
            }
        )
        assert ws.receive_json()["code"] == "session_mismatch"

    assert bus._queues[(Topic.AUDIO_IN, "test")].qsize() == 0


def test_misaligned_pcm_is_rejected(client: TestClient, bus: InMemoryEventBus) -> None:
    """홀수 바이트 PCM은 프레임이 어긋나 전 구간 잡음이 된다."""
    body = start_session(client)

    with client.websocket_connect(f"/v1/audio/stream?token={body['ws_token']}") as ws:
        ws.send_json(
            {
                "event": "audio_data",
                "session_id": body["session"]["session_id"],
                "format": "pcm_16k",
                "channel": "customer",
                "audio_base64": base64.b64encode(b"\x01\x02\x03").decode(),
            }
        )
        assert ws.receive_json()["code"] == "invalid_audio"

    assert bus._queues[(Topic.AUDIO_IN, "test")].qsize() == 0


def test_session_token_of_unknown_session_is_refused(client: TestClient) -> None:
    """서명이 유효해도 레지스트리에 없는 세션이면 붙일 수 없다."""
    token = issue_session_token("sess_ghost", "t1", SECRET)

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(f"/v1/audio/stream?token={token}"):
            pass
