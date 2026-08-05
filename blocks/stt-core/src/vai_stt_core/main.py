"""STT-CORE 진입점."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_stt_core.adapters import create_stt
from vai_stt_core.worker import BLOCK_ID, SttWorker

log = logging.getLogger(__name__)


class SttSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_STT_", extra="ignore")

    adapter: str = "fake"
    """운영 기본값은 faster_whisper. GPU 없는 개발/CI 환경을 위해 fake로 둔다."""

    model_path: str = ""
    adapter_config: str = "{}"
    workers: int = 1
    """한 프로세스에서 돌릴 소비 코루틴 수. GPU 1장에 과하게 늘리면 오히려 느려진다."""

    publish_ui: bool = True
    """FLT-MICRO 투입 후 False로 바꿔 마스킹 전 텍스트의 화면 노출을 막는다."""


def create_app() -> FastAPI:
    common = get_settings()
    stt_cfg = SttSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        adapter = create_stt(stt_cfg.adapter)
        await adapter.initialize(stt_cfg.model_path, json.loads(stt_cfg.adapter_config))
        log.info("STT 어댑터 로드", extra={"adapter": adapter.name})

        workers = [
            SttWorker(
                bus,
                adapter,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                publish_ui=stt_cfg.publish_ui,
            )
            for i in range(max(1, stt_cfg.workers))
        ]
        application.state.workers = workers
        tasks = [
            asyncio.create_task(w.run(), name=f"stt-worker-{i}") for i, w in enumerate(workers)
        ]
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
            await adapter.close()
            await bus.close()

    return create_block_app(
        block_id=BLOCK_ID, title="VisionAI STT-CORE", settings=common, lifespan=lifespan
    )


app = create_app()


def main() -> None:
    serve(app, default_port=8083)


if __name__ == "__main__":
    main()
