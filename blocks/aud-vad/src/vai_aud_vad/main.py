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
from vai_common.bus import build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_common.worker import SessionReaper

log = logging.getLogger(__name__)


class VadSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_VAD_", extra="ignore")

    adapter: str = "energy"
    adapter_config: str = "{}"
    """어댑터 초기화 JSON. 예: ``{"threshold": 0.6, "model_path": "/models/silero.jit"}``"""

    barge_in: bool = False
    """발화 시작 시 ``tts.cancel``을 낸다. **음성봇 구성에서만 켠다.**

    사람 상담원 구성에서 켜면 취소 이벤트가 통화 내내 흐르는데, 취소할 턴이
    없어 무해하긴 하지만 버스만 시끄러워진다. 봇이 없는 곳에서 켤 이유가 없다."""


def create_app() -> FastAPI:
    common = get_settings()
    vad_cfg = VadSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        vad = create_vad(vad_cfg.adapter)
        await vad.initialize(json.loads(vad_cfg.adapter_config))
        worker = VadWorker(
            bus,
            vad,
            group=common.consumer_group,
            consumer=common.consumer_name,
            barge_in=vad_cfg.barge_in,
        )
        application.state.worker = worker
        log.info("VAD 어댑터 로드", extra={"adapter": vad.name})

        reaper = SessionReaper(bus, [worker], group=f"{common.consumer_group}:vad-reaper")
        tasks = [
            asyncio.create_task(worker.run(), name="vad-worker"),
            asyncio.create_task(reaper.run(), name="vad-reaper"),
        ]
        try:
            yield
        finally:
            worker.stop()
            reaper.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await bus.close()

    return create_block_app(
        block_id=BLOCK_ID, title="VisionAI AUD-VAD", settings=common, lifespan=lifespan
    )


app = create_app()


def main() -> None:
    serve(app, default_port=8082)


if __name__ == "__main__":
    main()
