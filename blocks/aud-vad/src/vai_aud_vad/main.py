"""AUD-VAD 진입점.

워커 블록도 HTTP 서버를 함께 띄운다 — K8s 프로브와 메트릭 스크레이핑이
필요하기 때문이다. 실제 일은 lifespan이 띄우는 백그라운드 태스크가 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_aud_vad.adapters import create_vad
from vai_aud_vad.worker import BLOCK_ID, VadWorker
from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_contracts.events import SessionClosed
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)


class VadSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_VAD_", extra="ignore")

    adapter: str = "energy"
    adapter_config: str = "{}"
    """어댑터 초기화 JSON. 예: ``{"threshold": 0.6, "model_path": "/models/silero.jit"}``"""


def create_app() -> FastAPI:
    common = get_settings()
    vad_cfg = VadSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        vad = create_vad(vad_cfg.adapter)
        await vad.initialize(json.loads(vad_cfg.adapter_config))
        worker = VadWorker(bus, vad, group=common.consumer_group, consumer=common.consumer_name)
        application.state.worker = worker
        log.info("VAD 어댑터 로드", extra={"adapter": vad.name})

        reaper = _reap_sessions(bus, worker, common.consumer_group)
        tasks = [
            asyncio.create_task(worker.run(), name="vad-worker"),
            asyncio.create_task(reaper, name="vad-reaper"),
        ]
        try:
            yield
        finally:
            worker.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await bus.close()

    return create_block_app(
        block_id=BLOCK_ID, title="VisionAI AUD-VAD", settings=common, lifespan=lifespan
    )


async def _reap_sessions(bus: EventBus, worker: VadWorker, group: str) -> None:
    """세션 종료 이벤트를 받아 분할기 버퍼를 해제한다.

    별도 컨슈머 그룹을 쓴다 — 요약 블록(LLM-SUM)도 같은 토픽을 소비하므로
    그룹을 공유하면 둘 중 하나만 메시지를 받는다.
    """
    stream = bus.consume(
        Topic.SESSION_CLOSED,
        SessionClosed,
        group=f"{group}:vad-reaper",
        consumer="reaper-1",
    )
    async for delivery in stream:
        worker.release_session(delivery.event.session_id)
        await delivery.ack()


app = create_app()


def main() -> None:
    serve(app, default_port=8082)


if __name__ == "__main__":
    main()
