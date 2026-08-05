"""CORE-ADM — 운영 콘솔.

폐쇄망에서는 공급사가 원격으로 들여다볼 수 없다. 고객사 운영자가 스스로
"어느 블록이 죽었는지, 라이선스가 언제 만료되는지, 감사 로그가 온전한지"를
확인하지 못하면 곧바로 공급사 출동이 되고, 출동 원가가 유지보수 수익을 잠식한다.
이 화면은 편의 기능이 아니라 **원가 구조**다.

집계만 한다. 상태의 원본은 각 블록이 갖고 있고(``/readyz``, CORE-LIC, CORE-SEC),
콘솔은 그것을 모아 보여 줄 뿐이다. 콘솔이 상태를 따로 저장하면 곧 원본과 갈라져
"화면은 정상인데 실제로는 죽어 있는" 상태가 된다.

**콘솔이 죽어도 상담은 돈다.** 여기는 조회 전용이며 파이프라인 경로에 없다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.auth import AuthError, Principal, verify_access_token
from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction, AuditOutcome, AuditRecord, ChainStatus
from vai_contracts.ops import BlockHealth, PlatformStatus
from vai_core_adm import BLOCK_ID
from vai_core_adm.diagnose import (
    DIRECT_CALLS,
    ROUND_TRIPS,
    SAMPLE_TEXT,
    ProbeResult,
    probes,
    run_direct_call,
    run_round_trip,
)
from vai_core_adm.registry import DEFAULT_ENDPOINTS, probe_all

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

ADMIN_SCOPE = "admin"
"""운영 콘솔 접근에 필요한 스코프. 상담원 토큰으로는 들어올 수 없다."""


DEFAULT_SECRET = "dev-secret-change-me"
MIN_SECRET_BYTES = 32
"""HS256의 권장 최소 길이(RFC 7518 §3.2). 짧은 비밀키는 오프라인 무차별 대입에
취약하고, 관리 콘솔 토큰이 뚫리면 전사 감사 로그가 열린다."""


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_ADM_", extra="ignore")

    jwt_secret: str = DEFAULT_SECRET
    endpoints: str = ""
    """블록 → URL JSON. 비면 compose 기본 배치를 쓴다. 고객사마다 산 블록이
    다르므로 목록을 코드에 못 박지 않는다."""

    bus_url: str = "http://core-bus:8081"
    license_url: str = "http://core-lic:8095"
    security_url: str = "http://core-sec:8096"
    require_auth: bool = True
    """개발·데모에서만 끈다. 끄면 콘솔이 무인증으로 열린다."""


def _require_strong_secret(secret: str) -> None:
    """기본값·짧은 비밀키로는 기동하지 않는다.

    관리 콘솔 토큰을 위조할 수 있으면 전사 감사 로그와 라이선스 현황이 열린다.
    "나중에 바꾸겠다"로 남는 기본 비밀키가 이 영역의 가장 흔한 취약점이라,
    경고가 아니라 기동 거부로 막는다. 데모 구성은 ``VAI_ADM_REQUIRE_AUTH=false``로
    명시적으로 인증을 끄고 쓴다 — 끈 사실이 설정에 남는 편이 낫다.
    """
    if secret == DEFAULT_SECRET:
        raise RuntimeError(
            "VAI_ADM_JWT_SECRET이 기본값이다 — 운영 비밀키를 설정하거나 "
            "VAI_ADM_REQUIRE_AUTH=false로 인증을 명시적으로 꺼라"
        )
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise RuntimeError(
            f"VAI_ADM_JWT_SECRET이 너무 짧다 ({len(secret.encode())}바이트) — "
            f"HS256은 {MIN_SECRET_BYTES}바이트 이상을 권장한다(RFC 7518 §3.2)"
        )


def _endpoints(cfg: AdminSettings) -> dict[str, str]:
    if not cfg.endpoints:
        return dict(DEFAULT_ENDPOINTS)
    try:
        parsed = json.loads(cfg.endpoints)
    except ValueError:
        log.error("VAI_ADM_ENDPOINTS를 해석할 수 없다 — 기본 배치를 쓴다")
        return dict(DEFAULT_ENDPOINTS)
    return {str(k): str(v) for k, v in dict(parsed).items()}


def create_app(client: httpx.AsyncClient | None = None, bus: EventBus | None = None) -> FastAPI:
    common = get_settings()
    cfg = AdminSettings()
    injected = client is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.client = client or httpx.AsyncClient()
        application.state.bus = bus or build_bus(common.redis_url)
        try:
            yield
        finally:
            if not injected:
                await application.state.client.aclose()
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI CORE-ADM", settings=common, lifespan=lifespan
    )
    app.state.settings_adm = cfg
    app.state.endpoints = _endpoints(cfg)

    if not cfg.require_auth:
        log.warning("인증이 꺼진 채로 운영 콘솔이 열렸다 — 운영 구성이 아니다")
    else:
        _require_strong_secret(cfg.jwt_secret)

    async def operator(request: Request, authorization: str = Header(default="")) -> Principal:
        """운영자 인증. 게이트웨이와 **같은** 토큰 검증기를 쓴다.

        콘솔이 자체 인증을 따로 두면 스코프 해석이 갈라지고, 그 순간
        게이트웨이에서 막힌 권한이 콘솔에서는 통과한다.
        """
        cfg_: AdminSettings = request.app.state.settings_adm
        if not cfg_.require_auth:
            return Principal(tenant_id="*", subject="anonymous", scopes=frozenset({ADMIN_SCOPE}))

        token = authorization.removeprefix("Bearer ").strip()
        try:
            principal = verify_access_token(token, cfg_.jwt_secret)
            principal.require_scope(ADMIN_SCOPE)
        except AuthError as exc:
            # 거부된 시도야말로 감사에서 중요하다. 성공만 남기면 침해 시도가 안 보인다.
            await audit.emit(
                request.app.state.bus,
                action=AuditAction.ACCESS_DENIED,
                actor="unknown",
                block_id=BLOCK_ID,
                resource=request.url.path,
                outcome=AuditOutcome.DENIED,
                actor_ip=request.client.host if request.client else "",
                detail={"reason": str(exc)},
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        return principal

    @app.get("/v1/admin/status", response_model=PlatformStatus, tags=["ops"])
    async def platform_status(request: Request, _: Principal = Depends(operator)) -> PlatformStatus:
        """콘솔 첫 화면. 여기만 보고 "지금 정상인가"를 판단할 수 있어야 한다."""
        client_: httpx.AsyncClient = request.app.state.client
        blocks = await probe_all(client_, request.app.state.endpoints)
        cfg_: AdminSettings = request.app.state.settings_adm

        result = PlatformStatus(
            healthy=all(b.state == "ready" for b in blocks),
            ready_count=sum(1 for b in blocks if b.state == "ready"),
            total_count=len(blocks),
            blocks=blocks,
        )
        await _fill_license(client_, cfg_.license_url, result)
        await _fill_security(client_, cfg_.security_url, result)
        await _fill_capacity(client_, cfg_.bus_url, result)
        return result

    @app.get("/v1/admin/blocks", response_model=list[BlockHealth], tags=["ops"])
    async def blocks(request: Request, _: Principal = Depends(operator)) -> list[BlockHealth]:
        client_: httpx.AsyncClient = request.app.state.client
        return await probe_all(client_, request.app.state.endpoints)

    @app.get("/v1/admin/audit", response_model=list[AuditRecord], tags=["audit"])
    async def audit_query(
        request: Request,
        principal: Principal = Depends(operator),
        actor: str = "",
        action: AuditAction | None = None,
        tenant_id: str = "",
        session_id: str = "",
        since: datetime | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> list[AuditRecord]:
        """감사 조회 대리. CORE-SEC의 ``/internal``은 외부에 열지 않는다."""
        client_: httpx.AsyncClient = request.app.state.client
        cfg_: AdminSettings = request.app.state.settings_adm
        params: dict[str, str | int] = {"limit": limit}
        for key, value in (
            ("actor", actor),
            ("action", action.value if action else ""),
            ("tenant_id", tenant_id),
            ("session_id", session_id),
            ("since", since.isoformat() if since else ""),
        ):
            if value:
                params[key] = value

        try:
            response = await client_.get(
                f"{cfg_.security_url.rstrip('/')}/internal/v1/audit", params=params, timeout=10.0
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"감사 저장소에 연결할 수 없다: {exc}"
            ) from exc

        # 감사 로그 열람 자체가 감사 대상이다. 누가 무엇을 조회했는지 남지 않으면
        # 감사 기록을 훑어 본 사실이 흔적 없이 사라진다.
        await audit.emit(
            request.app.state.bus,
            action=AuditAction.EXPORT,
            actor=principal.subject,
            block_id=BLOCK_ID,
            resource="audit",
            tenant_id=tenant_id,
            actor_ip=request.client.host if request.client else "",
            detail={"filter_actor": actor, "limit": str(limit)},
        )
        return [AuditRecord.model_validate(item) for item in response.json()]

    @app.get("/v1/admin/audit/verify", response_model=ChainStatus, tags=["audit"])
    async def audit_verify(request: Request, _: Principal = Depends(operator)) -> ChainStatus:
        """무결성 검증 대리. 감사 대응의 첫 화면이다."""
        client_: httpx.AsyncClient = request.app.state.client
        cfg_: AdminSettings = request.app.state.settings_adm
        try:
            response = await client_.get(
                f"{cfg_.security_url.rstrip('/')}/internal/v1/audit/verify", timeout=30.0
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"감사 저장소에 연결할 수 없다: {exc}"
            ) from exc
        return ChainStatus.model_validate(response.json())

    # ── 블록 인터페이스 시험 ────────────────────────────────────────────────
    #
    # /readyz 는 프로세스가 살아 있다는 것만 말한다. 감사에서 나온 결함은
    # 전부 readyz 초록 상태에서 났다 — 살아 있는데 아무것도 못 하는 것이
    # 이 제품의 주된 실패 모양이다. 그래서 입력을 넣고 출력을 본다.

    @app.get("/v1/admin/diagnose", tags=["diagnose"])
    async def diagnose_list(_: Principal = Depends(operator)) -> list[dict[str, Any]]:
        """시험 가능한 항목. 카탈로그가 아니라 이 목록이 기준이다 —
        시험이 정의되지 않은 블록을 '통과'로 보이게 하지 않는다."""
        return probes()

    @app.post("/v1/admin/diagnose/{block_id}", response_model=ProbeResult, tags=["diagnose"])
    async def diagnose_block(
        block_id: str,
        request: Request,
        principal: Principal = Depends(operator),
        kind: str = Query("", description="round_trip | direct (비면 자동 선택)"),
        text: str = Query("", description="시험 입력. 비면 기본 문구"),
    ) -> ProbeResult:
        """블록 하나를 실제로 돌려 본다.

        **왕복 시험은 진짜 이벤트를 흘린다.** 뒤따르는 블록들도 그 이벤트를
        소비하므로 운영 중 실행은 그 사실을 알고 해야 한다. 세션 ID의
        ``diag-`` 접두사가 운영 데이터와 구분하는 유일한 표식이다.
        """
        wanted = block_id.upper()
        trip = ROUND_TRIPS.get(wanted)
        call = DIRECT_CALLS.get(wanted)
        if kind == "round_trip":
            call = None
        elif kind == "direct":
            trip = None
        if trip is None and call is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"{wanted}: 정의된 시험이 없다 — /v1/admin/diagnose 로 목록을 본다",
            )

        tenant = principal.tenant_id or "diag"
        if call is not None:
            endpoints: dict[str, str] = request.app.state.endpoints
            base = endpoints.get(call.block_id) or endpoints.get(call.endpoint_key)
            if not base:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"{call.block_id} 주소를 모른다 — VAI_ADM_ENDPOINTS 를 확인한다",
                )
            body = {"text": text} if text and "text" in call.body else {}
            if text and "query" in call.body:
                body = {"query": text}
            if text and "prompt" in call.body:
                body = {"prompt": text}
            return await run_direct_call(
                request.app.state.client, call, base, tenant_id=tenant, body=body
            )

        assert trip is not None
        bus_: EventBus = request.app.state.bus
        return await run_round_trip(bus_, trip, tenant_id=tenant, text=text or SAMPLE_TEXT)

    @app.get("/console", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "console.html")

    return app


async def _fill_license(client: httpx.AsyncClient, url: str, result: PlatformStatus) -> None:
    """라이선스 현황을 붙인다. 실패해도 나머지 화면은 그려야 한다."""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/internal/v1/license", timeout=httpx.Timeout(3.0)
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.warning("라이선스 현황 조회 실패", exc_info=True)
        return
    body = response.json()
    result.license_customer = str(body.get("customer_id", ""))
    result.license_expires = str(body.get("expires") or "")
    result.license_days_remaining = body.get("days_remaining")
    result.license_signature_verified = bool(body.get("signature_verified"))


async def _fill_capacity(client: httpx.AsyncClient, url: str, result: PlatformStatus) -> None:
    """동시 채널 사용률. 상한에 부딪히기 전에 보여야 예방 조치가 된다."""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/internal/v1/capacity", timeout=httpx.Timeout(3.0)
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.warning("채널 사용률 조회 실패", exc_info=True)
        return
    body = response.json()
    result.channels_active = body.get("active")
    result.channels_limit = body.get("limit")
    result.channels_limiting_block = str(body.get("limiting_block", ""))


async def _fill_security(client: httpx.AsyncClient, url: str, result: PlatformStatus) -> None:
    """감사 무결성과 암호화 상태를 붙인다."""
    base = url.rstrip("/")
    try:
        verified = await client.get(f"{base}/internal/v1/audit/verify", timeout=httpx.Timeout(10.0))
        verified.raise_for_status()
        chain = verified.json()
        result.audit_intact = bool(chain.get("intact"))
        result.audit_total = int(chain.get("total", 0))
        result.audit_broken_at = chain.get("broken_at")
    except httpx.HTTPError:
        log.warning("감사 무결성 조회 실패", exc_info=True)

    try:
        crypto = await client.get(f"{base}/internal/v1/crypto/status", timeout=httpx.Timeout(3.0))
        crypto.raise_for_status()
        body = crypto.json()
        result.encryption_enabled = bool(body.get("enabled"))
        result.encryption_algorithm = str(body.get("algorithm", ""))
    except httpx.HTTPError:
        log.warning("암호화 상태 조회 실패", exc_info=True)
