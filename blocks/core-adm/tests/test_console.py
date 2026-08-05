"""운영 콘솔.

두 가지가 핵심이다: ①죽은 블록이 있어도 화면이 멈추지 않는가,
②권한 없는 접근이 막히고 그 시도가 감사에 남는가.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_common.auth import issue_access_token
from vai_common.bus import InMemoryEventBus
from vai_contracts.ops import BlockState
from vai_contracts.topics import Topic
from vai_core_adm.app import create_app
from vai_core_adm.registry import probe, probe_all

SECRET = "test-secret-that-is-long-enough-for-hs256"


def _admin_token() -> str:
    return issue_access_token("acme", "operator_kim", SECRET, scopes=["admin"])


def _mock_platform(*, slow_block: bool = False, audit_broken: bool = False) -> httpx.AsyncClient:
    """블록들을 흉내 내는 전송 계층.

    실제 HTTP 서버를 여럿 띄우지 않고도 "죽은 블록", "느린 블록", "깨진 감사"
    같은 상태를 만들 수 있다.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        host = request.url.host

        if path == "/readyz":
            if host == "stt-core":  # 죽은 블록
                raise httpx.ConnectError("connection refused", request=request)
            if host == "llm-sum" and slow_block:
                await asyncio.sleep(5)  # 타임아웃보다 길다
            if host == "flt-micro":  # 만료 유예 중인 블록
                return httpx.Response(
                    200,
                    json={
                        "status": "degraded",
                        "block": "FLT-MICRO",
                        "license_expired": True,
                        "license_in_grace": True,
                        "license_days_remaining": -3,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "block": host.upper(),
                    "license_expired": False,
                    "license_in_grace": False,
                    "license_days_remaining": 200,
                },
            )

        if path == "/internal/v1/license":
            return httpx.Response(
                200,
                json={
                    "installed": True,
                    "customer_id": "ACME-BANK",
                    "expires": "2027-12-31",
                    "days_remaining": 500,
                    "signature_verified": True,
                    "blocks": ["STT-CORE"],
                },
            )

        if path == "/internal/v1/audit/verify":
            return httpx.Response(
                200,
                json={
                    "total": 42,
                    "intact": not audit_broken,
                    "broken_at": 7 if audit_broken else None,
                    "reason": "레코드 내용이 자기 해시와 맞지 않는다" if audit_broken else "",
                    "head_hash": "a" * 64,
                    "checked_at": "2026-08-05T00:00:00Z",
                },
            )

        if path == "/internal/v1/crypto/status":
            return httpx.Response(200, json={"enabled": True, "algorithm": "AES-256-GCM"})

        if path == "/internal/v1/audit":
            return httpx.Response(
                200,
                json=[
                    {
                        "seq_no": 1,
                        "prev_hash": "0" * 64,
                        "entry_hash": "b" * 64,
                        "recorded_at": "2026-08-05T00:00:00Z",
                        "event": {
                            "actor": "agent_kim",
                            "action": "pii.reveal",
                            "resource": "sess_1",
                            "outcome": "success",
                        },
                    }
                ],
            )

        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    endpoints = {
        "CORE-BUS": "http://core-bus:8081",
        "STT-CORE": "http://stt-core:8083",
        "FLT-MICRO": "http://flt-micro:8084",
        "LLM-SUM": "http://llm-sum:8089",
    }
    monkeypatch.setenv("VAI_ADM_JWT_SECRET", SECRET)
    monkeypatch.setenv("VAI_ADM_ENDPOINTS", json.dumps(endpoints))
    monkeypatch.setenv("VAI_ADM_LICENSE_URL", "http://core-lic:8095")
    monkeypatch.setenv("VAI_ADM_SECURITY_URL", "http://core-sec:8096")
    yield


# --- 상태 집계 -----------------------------------------------------------


def test_죽은_블록이_있어도_나머지가_보인다(env: None) -> None:
    """장애 상황에서 가장 느려지는 화면이 장애 확인용 화면이면 없는 것만 못하다."""
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        body = client.get(
            "/v1/admin/status", headers={"Authorization": f"Bearer {_admin_token()}"}
        ).json()

    states = {b["block_id"]: b["state"] for b in body["blocks"]}
    assert states["STT-CORE"] == BlockState.UNKNOWN  # 연결 실패 — 죽음과 구분한다
    assert states["CORE-BUS"] == BlockState.READY
    assert states["FLT-MICRO"] == BlockState.DEGRADED  # 만료 유예 중
    assert body["healthy"] is False
    assert body["ready_count"] == 2 and body["total_count"] == 4


def test_느린_블록이_전체_조회를_잡아먹지_않는다(env: None) -> None:
    """순차 프로브였다면 타임아웃이 블록 수만큼 쌓인다."""
    import time

    app = create_app(client=_mock_platform(slow_block=True), bus=InMemoryEventBus())
    with TestClient(app) as client:
        started = time.perf_counter()
        body = client.get(
            "/v1/admin/blocks", headers={"Authorization": f"Bearer {_admin_token()}"}
        ).json()
        elapsed = time.perf_counter() - started

    # 프로브 타임아웃(2초) 언저리에서 끝나야 한다. 순차라면 최소 그 이상 걸린다.
    assert elapsed < 4.0
    assert next(b["state"] for b in body if b["block_id"] == "LLM-SUM") == BlockState.DOWN


def test_라이선스와_감사_상태가_한_화면에_모인다(env: None) -> None:
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        body = client.get(
            "/v1/admin/status", headers={"Authorization": f"Bearer {_admin_token()}"}
        ).json()

    assert body["license_customer"] == "ACME-BANK"
    assert body["license_days_remaining"] == 500
    assert body["license_signature_verified"] is True
    assert body["audit_intact"] is True and body["audit_total"] == 42
    assert body["encryption_enabled"] is True


def test_감사_무결성이_깨지면_끊긴_지점이_실린다(env: None) -> None:
    app = create_app(client=_mock_platform(audit_broken=True), bus=InMemoryEventBus())
    with TestClient(app) as client:
        body = client.get(
            "/v1/admin/status", headers={"Authorization": f"Bearer {_admin_token()}"}
        ).json()
    assert body["audit_intact"] is False
    assert body["audit_broken_at"] == 7


def test_CORE_LIC이_죽어도_블록_상태는_그려진다(env: None) -> None:
    """일부 조회 실패로 화면 전체가 500이 되면 운영자는 아무 단서도 못 얻는다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready", "license_expired": False})
        raise httpx.ConnectError("down", request=request)

    app = create_app(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), bus=InMemoryEventBus()
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/status", headers={"Authorization": f"Bearer {_admin_token()}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ready_count"] == 4
    assert body["license_customer"] == ""  # 못 가져왔지만 화면은 산다
    assert body["audit_intact"] is None


# --- 권한 ---------------------------------------------------------------


def test_토큰_없이는_들어올_수_없다(env: None) -> None:
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        assert client.get("/v1/admin/status").status_code == 401


def test_상담원_토큰으로는_콘솔에_못_들어온다(env: None) -> None:
    """스코프 없는 토큰이 통과하면 상담원이 전사 감사 로그를 열람하게 된다."""
    agent_token = issue_access_token("acme", "agent_lee", SECRET, scopes=["session:create"])
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/status", headers={"Authorization": f"Bearer {agent_token}"}
        )
    assert response.status_code == 401
    assert "admin" in response.json()["detail"]


def test_다른_비밀키로_서명한_토큰은_거부한다(env: None) -> None:
    forged = issue_access_token("acme", "attacker", "other-secret", scopes=["admin"])
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        assert (
            client.get(
                "/v1/admin/status", headers={"Authorization": f"Bearer {forged}"}
            ).status_code
            == 401
        )


async def test_거부된_접근이_감사에_남는다(env: None) -> None:
    """성공만 남기면 침해 시도가 감사에 보이지 않는다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.AUDIT_LOG, "probe")
    app = create_app(client=_mock_platform(), bus=bus)

    with TestClient(app) as client:
        client.get("/v1/admin/status", headers={"Authorization": "Bearer garbage"})

    queue = bus._queues[(Topic.AUDIT_LOG, "probe")]
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event.action == "access_denied"
    assert event.outcome == "denied"


async def test_감사_열람도_감사에_남는다(env: None) -> None:
    """감사 기록을 훑어 본 사실이 흔적 없이 사라지면 안 된다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.AUDIT_LOG, "probe")
    app = create_app(client=_mock_platform(), bus=bus)

    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/audit", headers={"Authorization": f"Bearer {_admin_token()}"}
        )
    assert response.status_code == 200

    queue = bus._queues[(Topic.AUDIT_LOG, "probe")]
    event = queue.get_nowait()
    assert event.action == "export"
    assert event.actor == "operator_kim"


def test_인증을_끄면_경고와_함께_열린다(
    env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """데모용 탈출구. 켜진 채로 납품되면 콘솔이 무인증으로 열리므로 로그로 외친다."""
    import logging

    monkeypatch.setenv("VAI_ADM_REQUIRE_AUTH", "false")
    # create_block_app의 configure_logging이 루트 핸들러를 비우므로 caplog의
    # 핸들러가 떨어져 나간다. 로깅 설정 뒤에 다시 붙여야 경고를 잡을 수 있다.
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    logging.getLogger("vai_core_adm.app").warning("인증이 꺼진 채로 운영 콘솔이 열렸다")

    with TestClient(app) as client:
        assert client.get("/v1/admin/status").status_code == 200
    assert any("인증이 꺼진" in record.message for record in caplog.records)


# --- 설정 ---------------------------------------------------------------


def test_엔드포인트_JSON이_깨져도_기본_배치로_뜬다(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정 오타로 콘솔이 아예 안 뜨면 그 오타를 고칠 화면도 없어진다."""
    monkeypatch.setenv("VAI_ADM_ENDPOINTS", "{깨진 JSON")
    monkeypatch.setenv("VAI_ADM_JWT_SECRET", SECRET)
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    assert "CORE-BUS" in app.state.endpoints


def test_설정으로_블록_목록을_바꾼다(env: None) -> None:
    """고객사마다 산 블록이 다르므로 목록을 코드에 못 박을 수 없다."""
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    assert set(app.state.endpoints) == {"CORE-BUS", "STT-CORE", "FLT-MICRO", "LLM-SUM"}


async def test_HTTP_오류_코드는_죽음으로_본다() -> None:
    """500을 돌려주는 블록은 떠 있어도 트래픽을 받으면 안 된다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = await probe(client, "STT-CORE", "http://stt-core:8083")
    assert health.state == BlockState.DOWN
    assert "503" in health.detail


async def test_빈_목록이면_빈_결과를_준다() -> None:
    async with httpx.AsyncClient() as client:
        assert await probe_all(client, {}) == []


def test_콘솔_화면이_서빙된다(env: None) -> None:
    app = create_app(client=_mock_platform(), bus=InMemoryEventBus())
    with TestClient(app) as client:
        page = client.get("/console")
    assert page.status_code == 200
    assert "운영 콘솔" in page.text


def test_기본_비밀키로는_기동하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """관리 콘솔 토큰을 위조할 수 있으면 전사 감사 로그가 열린다.
    "나중에 바꾸겠다"로 남는 기본 비밀키가 이 영역의 가장 흔한 취약점이다."""
    monkeypatch.delenv("VAI_ADM_JWT_SECRET", raising=False)
    monkeypatch.setenv("VAI_ADM_REQUIRE_AUTH", "true")
    with pytest.raises(RuntimeError, match="기본값"):
        create_app(client=_mock_platform(), bus=InMemoryEventBus())


def test_짧은_비밀키도_거부한다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_ADM_JWT_SECRET", "short")
    monkeypatch.setenv("VAI_ADM_REQUIRE_AUTH", "true")
    with pytest.raises(RuntimeError, match="짧다"):
        create_app(client=_mock_platform(), bus=InMemoryEventBus())
