"""AVA-COUNSEL — 아바타 가상상담원 블록.

클라이언트(브라우저·키오스크)는 WebSocket 하나로 **오디오와 입 모양을 함께**
받는다. 두 소켓으로 나누면 연결 지연이 서로 달라 시작부터 어긋난다.

아바타 렌더링은 클라이언트가 한다. 서버가 영상을 만들면 세션마다 GPU가 필요하고,
인코딩·전송 지연이 음성 위에 더해진다 — 온프레미스에서 통화 100개를 받으려면
GPU 100장이 필요하다는 뜻이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_ava_counsel import BLOCK_ID
from vai_ava_counsel.viseme import build_track
from vai_ava_counsel.worker import AvatarWorker
from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.avatar import AvatarState, AvatarStatus, AvatarTrack
from vai_contracts.events import BaseEvent
from vai_contracts.speech import SpeechChunk
from vai_contracts.topics import Topic

EventT = TypeVar("EventT", bound=BaseEvent)

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class AvatarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_AVA_", extra="ignore")

    workers: int = 2


def create_app(bus: EventBus | None = None) -> FastAPI:
    common = get_settings()
    cfg = AvatarSettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.bus = bus or build_bus(common.redis_url)
        workers = [
            AvatarWorker(
                application.state.bus,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
            )
            for i in range(max(1, cfg.workers))
        ]
        tasks = [asyncio.create_task(w.run(), name=f"ava-{i}") for i, w in enumerate(workers)]
        try:
            yield
        finally:
            for worker in workers:
                worker.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if not injected:
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI AVA-COUNSEL", settings=common, lifespan=lifespan
    )

    @app.websocket("/v1/avatar/stream")
    async def stream(websocket: WebSocket, session_id: str = "") -> None:
        """아바타 클라이언트 소켓. 오디오와 입 모양을 **한 소켓으로** 보낸다.

        두 소켓으로 나누면 연결 지연이 서로 달라 시작부터 어긋나고,
        그 어긋남은 사람이 즉시 알아챈다.
        """
        if not session_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="session_id 필요")
            return

        await websocket.accept()
        bus_: EventBus = websocket.app.state.bus
        # 화면마다 별도 컨슈머 그룹. 같은 그룹을 공유하면 화면 두 개가
        # 프레임을 나눠 갖게 되어 각자 절반만 움직인다.
        group = f"avatar:{session_id}"

        audio = asyncio.create_task(
            _relay(bus_, websocket, session_id, Topic.TTS_AUDIO, SpeechChunk, group, "audio")
        )
        track = asyncio.create_task(
            _relay(bus_, websocket, session_id, Topic.AVATAR_TRACK, AvatarTrack, group, "track")
        )
        try:
            await asyncio.gather(audio, track)
        except WebSocketDisconnect:
            log.info("아바타 화면 종료", extra={"session_id": session_id})
        except Exception:
            log.exception("아바타 소켓 오류", extra={"session_id": session_id})
        finally:
            for task in (audio, track):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @app.post("/internal/v1/avatar/state", tags=["avatar"])
    async def set_state(payload: AvatarStatus, request: Request) -> dict[str, str]:
        """상태 전환 — 듣는 중·생각 중.

        **가만히 멈춰 있으면 안 된다.** 정지한 아바타는 화면이 얼어붙은 것처럼
        보이고, 고객은 통화가 끊겼다고 여긴다.
        """
        bus_: EventBus = request.app.state.bus
        await bus_.publish_ui(payload.session_id, payload)
        return {"state": payload.state.value}

    @app.post("/internal/v1/avatar/preview", tags=["avatar"])
    async def preview(text: str, duration_ms: int = 1000) -> dict[str, object]:
        """입 모양 미리보기 — 저작 도구가 문구를 다듬을 때 쓴다."""
        frames = build_track(text, duration_ms)
        return {
            "text": text,
            "duration_ms": duration_ms,
            "frames": [f.model_dump(mode="json") for f in frames],
        }

    @app.get("/avatar", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "avatar.html")

    return app


async def _relay(
    bus: EventBus,
    websocket: WebSocket,
    session_id: str,
    topic: Topic,
    model: type[EventT],
    group: str,
    kind: str,
) -> None:
    """토픽을 구독해 이 세션 것만 소켓으로 넘긴다."""
    stream = bus.consume(topic, model, group=f"{group}:{kind}", consumer="screen")
    async for delivery in stream:
        await delivery.ack()
        event = delivery.event
        if event.session_id != session_id:
            continue
        payload = event.model_dump(mode="json")
        payload["kind"] = kind
        await websocket.send_json(payload)


__all__ = ["AvatarState", "create_app"]
