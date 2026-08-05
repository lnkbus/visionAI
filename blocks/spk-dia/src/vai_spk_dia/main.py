"""SPK-DIA 진입점."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_common.worker import SessionReaper
from vai_spk_dia.diarizer import DiarizerConfig
from vai_spk_dia.embedding import create_embedder
from vai_spk_dia.worker import BLOCK_ID, DiarizationWorker

log = logging.getLogger(__name__)


class DiaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_DIA_", extra="ignore")

    embedder: str = "spectral"
    """운영 기본값은 speechbrain. GPU·모델 없는 개발/CI를 위해 spectral로 둔다."""

    embedder_model: str = ""
    embedder_config: str = "{}"
    similarity_threshold: float = 0.72
    """임베더를 바꾸면 반드시 재조정한다. 임베더마다 유사도 분포가 다르다."""

    max_speakers: int = 12
    min_duration_ms: int = 700


def create_app() -> FastAPI:
    common = get_settings()
    cfg = DiaSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        embedder = create_embedder(cfg.embedder)
        await embedder.initialize(cfg.embedder_model, json.loads(cfg.embedder_config))
        log.info("화자 임베더 로드", extra={"embedder": embedder.name})

        worker = DiarizationWorker(
            bus,
            embedder,
            group=common.consumer_group,
            consumer=common.consumer_name,
            config=DiarizerConfig(
                similarity_threshold=cfg.similarity_threshold,
                max_speakers=cfg.max_speakers,
                min_duration_ms=cfg.min_duration_ms,
            ),
        )
        application.state.worker = worker
        reaper = SessionReaper(bus, [worker], group=f"{common.consumer_group}:dia-reaper")
        tasks = [
            asyncio.create_task(worker.run(), name="spk-dia-worker"),
            asyncio.create_task(reaper.run(), name="spk-dia-reaper"),
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
            await embedder.close()
            await bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI SPK-DIA", settings=common, lifespan=lifespan
    )

    @app.get("/internal/v1/sessions/{session_id}/speakers", tags=["diarization"])
    async def speakers(session_id: str, request: Request) -> list[dict[str, object]]:
        """세션의 참석자 요약. 회의록 참석자 목록의 근거다."""
        worker: DiarizationWorker = request.app.state.worker
        if session_id not in worker._sessions:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없다")
        return worker.diarizer(session_id).summary()

    return app


app = create_app()


def main() -> None:
    serve(app, default_port=8093)


if __name__ == "__main__":
    main()
