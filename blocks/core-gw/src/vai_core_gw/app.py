"""CORE-GW 애플리케이션."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.auth import (
    AuthError,
    Principal,
    issue_session_token,
    verify_access_token,
    verify_session_token,
)
from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app, set_landing
from vai_common.settings import get_settings
from vai_contracts.session import AudioFormat, Session, SessionCreate, SessionProfile, SessionState
from vai_core_gw.bus_client import CoreBusClient, CoreBusError
from vai_core_gw.stream import AudioStreamHandler

log = logging.getLogger(__name__)
BLOCK_ID = "CORE-GW"
STATIC_DIR = Path(__file__).parent / "static"


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_GW_", extra="ignore")

    jwt_secret: str = "dev-only-secret-change-me"
    dev_auth: bool = True
    """True면 액세스 토큰 없이도 세션을 만들 수 있다. 운영 배포에서는 반드시 False."""

    demo_enabled: bool = True
    """마이크→자막 데모 페이지 노출 여부. 온프렘 운영 배포에서는 끈다."""


class StartSessionRequest(BaseModel):
    tenant_id: str = "demo"
    profile: SessionProfile = SessionProfile.AICC
    audio_format: AudioFormat = AudioFormat.PCM_16K
    language: str = "ko"


class StartSessionResponse(BaseModel):
    session: Session
    ws_token: str
    """``/v1/audio/stream?token=`` 에 그대로 넣는다."""


def get_core_bus(request: Request) -> CoreBusClient:
    return request.app.state.core_bus  # type: ignore[no-any-return]


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus  # type: ignore[no-any-return]


def create_app(
    *,
    bus: EventBus | None = None,
    core_bus: CoreBusClient | None = None,
    gw_settings: GatewaySettings | None = None,
) -> FastAPI:
    """게이트웨이 앱을 만든다.

    ``bus``/``core_bus``를 넘기면 그 인스턴스를 쓰고 소유권도 호출자에게 남는다
    (테스트가 인메모리 버스와 ASGI 클라이언트를 물릴 때 쓴다).
    """
    common = get_settings()
    gw_cfg = gw_settings or GatewaySettings()
    injected = bus is not None or core_bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.bus = bus or build_bus(common.redis_url)
        application.state.core_bus = core_bus or CoreBusClient(common.core_bus_url)
        try:
            yield
        finally:
            if not injected:
                await application.state.core_bus.aclose()
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI CORE-GW", settings=common, lifespan=lifespan
    )
    app.state.gw_settings = gw_cfg

    def authenticate(authorization: str | None = Header(default=None)) -> Principal:
        if authorization and authorization.lower().startswith("bearer "):
            try:
                return verify_access_token(authorization.split(" ", 1)[1], gw_cfg.jwt_secret)
            except AuthError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        if gw_cfg.dev_auth:
            return Principal(tenant_id="demo", subject="dev", scopes=frozenset({"session:create"}))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="인증이 필요하다")

    @app.post(
        "/v1/sessions",
        response_model=StartSessionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["sessions"],
    )
    async def start_session(
        payload: StartSessionRequest,
        principal: Principal = Depends(authenticate),
        core_bus: CoreBusClient = Depends(get_core_bus),
    ) -> StartSessionResponse:
        """세션을 만들고 WebSocket 접속용 단기 토큰을 함께 돌려준다."""
        try:
            principal.require_scope("session:create")
        except AuthError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        # 테넌트는 토큰에서만 온다. 본문의 tenant_id를 신뢰하면 교차 테넌트 접근이 열린다.
        tenant_id = principal.tenant_id
        try:
            session = await core_bus.create_session(
                SessionCreate(
                    tenant_id=tenant_id,
                    profile=payload.profile,
                    audio_format=payload.audio_format,
                    language=payload.language,
                )
            )
        except CoreBusError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="세션 서비스에 연결할 수 없다"
            ) from exc

        return StartSessionResponse(
            session=session,
            ws_token=issue_session_token(session.session_id, tenant_id, gw_cfg.jwt_secret),
        )

    @app.post(
        "/v1/sessions/{session_id}/join",
        response_model=StartSessionResponse,
        tags=["sessions"],
    )
    async def join_session(
        session_id: str,
        principal: Principal = Depends(authenticate),
        core_bus: CoreBusClient = Depends(get_core_bus),
    ) -> StartSessionResponse:
        """이미 도는 세션에 **입력을 하나 더** 붙인다.

        상담도 회의도 여러 사람이 한다. 그런데 세션을 만드는 길만 있으면
        화면 하나가 곧 세션 하나가 된다 — 상담원과 고객이 각자 마이크를 켜면
        서로 다른 상담 두 건이 되고, 화면에는 늘 한쪽만 뜬다. 실제로 그렇게
        보였다: 두 사람이 말하는데 자막의 화자가 전부 "고객"이었다.

        토큰은 새로 발급하되 **세션은 그대로**다. 그래야 두 마이크가 한
        타임라인에 모이고, 요약도 한 벌만 나온다.
        """
        try:
            principal.require_scope("session:create")
        except AuthError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        try:
            session = await core_bus.get_session(session_id)
        except CoreBusError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="세션 서비스에 연결할 수 없다"
            ) from exc

        if session is None or session.tenant_id != principal.tenant_id:
            # 다른 테넌트의 세션은 "권한 없음"이 아니라 "없음"으로 답한다.
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없다")

        if session.state is not SessionState.ACTIVE:
            # 끝난 세션에 붙으면 소켓은 열리는데 자막은 영영 안 나온다.
            # 그 조용한 실패보다 여기서 이유를 말하고 막는 편이 낫다.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"이미 끝난 세션이다 (상태: {session.state}) — 새 세션을 시작한다",
            )

        return StartSessionResponse(
            session=session,
            ws_token=issue_session_token(session.session_id, session.tenant_id, gw_cfg.jwt_secret),
        )

    @app.post("/v1/sessions/{session_id}/close", response_model=Session, tags=["sessions"])
    async def close_session(
        session_id: str,
        principal: Principal = Depends(authenticate),
        core_bus: CoreBusClient = Depends(get_core_bus),
    ) -> Session:
        session = await core_bus.get_session(session_id)
        if session is None or session.tenant_id != principal.tenant_id:
            # 다른 테넌트의 세션은 "권한 없음"이 아니라 "없음"으로 답한다.
            # 존재 여부 자체가 정보이기 때문이다.
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없다")
        closed = await core_bus.close_session(session_id)
        assert closed is not None
        return closed

    @app.websocket("/v1/audio/stream")
    async def audio_stream(websocket: WebSocket, token: str = "") -> None:
        """사양서 §4 오디오 스트리밍 프로토콜.

        WebSocket 라우트는 ``Request``가 아니라 ``WebSocket``을 받으므로
        REST용 ``Depends`` 의존성을 그대로 쓸 수 없다. 상태는 앱에서 직접 꺼낸다.
        """
        bus: EventBus = websocket.app.state.bus
        core_bus: CoreBusClient = websocket.app.state.core_bus
        try:
            claims = verify_session_token(token, gw_cfg.jwt_secret)
        except AuthError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
            return

        session = await core_bus.get_session(claims.session_id)
        if session is None or session.state is not SessionState.ACTIVE:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="inactive session")
            return

        await websocket.accept()
        log.info(
            "오디오 스트림 시작",
            extra={"session_id": session.session_id, "profile": session.profile.value},
        )
        handler = AudioStreamHandler(websocket, session, bus)
        try:
            await handler.run()
        except WebSocketDisconnect:
            pass
        finally:
            # 클라이언트가 명시적으로 닫았을 때만 세션을 종료한다. 네트워크 단절은
            # 재접속 가능성이 있으므로 세션을 살려 둔다(요약이 조기에 돌면 안 된다).
            if handler.should_close_session:
                await core_bus.close_session(session.session_id)

    if gw_cfg.demo_enabled:

        @app.get("/demo", include_in_schema=False)
        async def demo_page() -> FileResponse:
            return FileResponse(STATIC_DIR / "demo.html")

        set_landing(app, "/demo")

    return app
