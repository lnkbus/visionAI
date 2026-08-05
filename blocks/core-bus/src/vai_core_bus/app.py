"""CORE-BUS 내부 API.

``/internal/`` 접두사는 클러스터 내부 전용이라는 뜻이다. 외부 트래픽은 항상
CORE-GW를 거치며, 이 API는 서비스 메시/네트워크 정책으로 외부 노출을 막는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, status

from vai_common.bus import EventBus, InMemoryEventBus, RedisEventBus
from vai_common.service import create_block_app
from vai_common.settings import CommonSettings, get_settings
from vai_contracts.events import SessionClosed
from vai_contracts.session import Session, SessionCreate, SessionState
from vai_contracts.topics import Topic
from vai_core_bus.store import InMemorySessionStore, RedisSessionStore, SessionStore

log = logging.getLogger(__name__)
BLOCK_ID = "CORE-BUS"


def new_session_id() -> str:
    """``sess_20260731_ab12cd34`` 형태. 날짜 접두사는 운영 중 로그 추적에 유용하다."""
    return f"sess_{datetime.now(UTC):%Y%m%d}_{uuid.uuid4().hex[:8]}"


def get_store(request: Request) -> SessionStore:
    return request.app.state.store  # type: ignore[no-any-return]


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus  # type: ignore[no-any-return]


def create_app(
    settings: CommonSettings | None = None,
    *,
    bus: EventBus | None = None,
    store: SessionStore | None = None,
) -> FastAPI:
    """세션 레지스트리 앱을 만든다.

    ``bus``/``store``를 넘기면 소유권은 호출자에게 남는다(테스트용 주입 통로).
    """
    cfg = settings or get_settings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if bus is not None or store is not None:
            application.state.bus = bus or InMemoryEventBus()
            application.state.store = store or InMemorySessionStore()
        elif cfg.redis_url:
            redis_bus = RedisEventBus.from_url(cfg.redis_url)
            application.state.bus = redis_bus
            application.state.store = RedisSessionStore(redis_bus.redis)
        else:
            application.state.bus = InMemoryEventBus()
            application.state.store = InMemorySessionStore()
        try:
            yield
        finally:
            if not injected:
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI CORE-BUS", settings=cfg, lifespan=lifespan
    )

    @app.post(
        "/internal/v1/sessions",
        response_model=Session,
        status_code=status.HTTP_201_CREATED,
        tags=["sessions"],
    )
    async def create_session(
        payload: SessionCreate, store: SessionStore = Depends(get_store)
    ) -> Session:
        session = Session(
            session_id=new_session_id(),
            tenant_id=payload.tenant_id,
            profile=payload.profile,
            audio_format=payload.audio_format,
            language=payload.language,
            metadata=payload.metadata,
        )
        await store.save(session)
        log.info(
            "세션 생성",
            extra={
                "session_id": session.session_id,
                "tenant_id": session.tenant_id,
                "profile": session.profile.value,
            },
        )
        return session

    @app.get("/internal/v1/sessions/{session_id}", response_model=Session, tags=["sessions"])
    async def get_session(session_id: str, store: SessionStore = Depends(get_store)) -> Session:
        session = await store.get(session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없다")
        return session

    @app.post("/internal/v1/sessions/{session_id}/close", response_model=Session, tags=["sessions"])
    async def close_session(
        session_id: str,
        store: SessionStore = Depends(get_store),
        bus: EventBus = Depends(get_bus),
    ) -> Session:
        """세션을 닫고 배치 요약을 트리거한다."""
        before = await store.get(session_id)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없다")
        already_closed = before.state is SessionState.CLOSED

        session = await store.close(session_id)
        assert session is not None

        # 중복 종료 요청에도 session.closed는 한 번만 나가야 한다.
        # 요약 배치가 두 번 도는 것은 GPU 낭비이자 중복 산출물의 원인이다.
        if not already_closed:
            duration_ms = int(
                ((session.closed_at or datetime.now(UTC)) - session.created_at).total_seconds()
                * 1000
            )
            await bus.publish(
                Topic.SESSION_CLOSED,
                SessionClosed(
                    session_id=session.session_id,
                    tenant_id=session.tenant_id,
                    profile=session.profile,
                    duration_ms=duration_ms,
                ),
            )
            log.info("세션 종료", extra={"session_id": session_id, "duration_ms": duration_ms})
        return session

    return app
