"""UI-AGENT — 상담원 워크스페이스.

상담원이 실제로 보는 화면이다. 지금까지 만든 모든 블록의 산출물이 여기서 만난다:
실시간 자막(마스킹본), 컴플라이언스 경고, 지식 팝업, 상담 종료 후 요약.

**팝업 채택 피드백**이 이 블록의 숨은 핵심이다. 상담원이 팝업을 실제로 썼는지가
품질 개선의 유일한 객관 신호인데, 그걸 수집할 지점은 상담원 화면뿐이다.
피드백 버튼이 없으면 SCN-STUDIO의 채택률 화면은 영원히 비어 있다.

이 블록은 **읽기 전용 관찰자**다. 오디오를 밀어 넣지 않고 세션의 UI 채널을
구독만 한다. 상담원 화면이 죽어도 상담 파이프라인은 그대로 돈다.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.authoring import PopupFeedback
from vai_contracts.summary import Summary

log = logging.getLogger(__name__)
BLOCK_ID = "UI-AGENT"
STATIC_DIR = Path(__file__).parent / "static"


class AgentUiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_AGENTUI_", extra="ignore")

    studio_url: str = "http://localhost:8090"
    """SCN-STUDIO. 팝업 피드백을 여기로 넘긴다."""

    summary_url: str = "http://localhost:8089"
    """LLM-SUM. 상담 종료 후 요약을 조회한다."""

    gateway_url: str = "http://localhost:8080"
    """CORE-GW. 데모용 세션 생성 프록시."""


class FeedbackAck(BaseModel):
    status: str = "recorded"


def create_app(
    bus: EventBus | None = None,
    studio_client: httpx.AsyncClient | None = None,
    summary_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    common = get_settings()
    cfg = AgentUiSettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.bus = bus or build_bus(common.redis_url)
        application.state.studio = studio_client or httpx.AsyncClient(
            base_url=cfg.studio_url.rstrip("/"), timeout=5.0
        )
        application.state.summary = summary_client or httpx.AsyncClient(
            base_url=cfg.summary_url.rstrip("/"), timeout=5.0
        )
        try:
            yield
        finally:
            if not injected:
                await application.state.studio.aclose()
                await application.state.summary.aclose()
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI UI-AGENT", settings=common, lifespan=lifespan
    )

    @app.websocket("/v1/agent/stream")
    async def observe(websocket: WebSocket, session_id: str = "") -> None:
        """세션 관찰 소켓.

        읽기 전용이다. 상담원 화면은 오디오를 밀어 넣지 않으며, 여기서 끊겨도
        상담 파이프라인에는 아무 영향이 없다.
        """
        if not session_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="session_id 필요")
            return

        await websocket.accept()
        log.info("상담원 화면 연결", extra={"session_id": session_id})
        bus_: EventBus = websocket.app.state.bus
        try:
            async for payload in bus_.subscribe_ui(session_id):
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            log.info("상담원 화면 종료", extra={"session_id": session_id})
        except Exception:
            log.exception("관찰 소켓 오류", extra={"session_id": session_id})
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)

    @app.post("/v1/agent/feedback", response_model=FeedbackAck, tags=["agent"])
    async def submit_feedback(payload: PopupFeedback, request: Request) -> FeedbackAck:
        """팝업 채택 여부를 기록한다.

        실패해도 상담원에게 오류를 보이지 않는다 — 피드백 수집은 부가 기능이고,
        여기서 에러 모달이 뜨면 상담원은 다음부터 버튼을 누르지 않는다.
        """
        client: httpx.AsyncClient = request.app.state.studio
        try:
            response = await client.post(
                "/internal/v1/feedback", json=payload.model_dump(mode="json")
            )
            response.raise_for_status()
        except httpx.HTTPError:
            log.warning("피드백 전달 실패", exc_info=True)
            return FeedbackAck(status="deferred")
        return FeedbackAck()

    @app.get("/v1/agent/summary/{session_id}", response_model=Summary, tags=["agent"])
    async def get_summary(session_id: str, request: Request) -> Summary:
        """상담 종료 후 요약. 아직 생성 중이면 404가 온다(화면이 재시도한다)."""
        client: httpx.AsyncClient = request.app.state.summary
        try:
            response = await client.get(f"/internal/v1/summaries/{session_id}")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="요약 서비스에 연결할 수 없다"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약이 아직 없다")
        response.raise_for_status()
        return Summary.model_validate(response.json())

    @app.get("/workspace", include_in_schema=False)
    async def workspace() -> FileResponse:
        return FileResponse(STATIC_DIR / "workspace.html")

    return app
