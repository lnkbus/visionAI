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

from vai_common.bus import RedisEventBus, build_bus
from vai_common.config_store import (
    CachedConfig,
    ConfigKind,
    ConfigStore,
    InMemoryConfigStore,
    RedisConfigStore,
)
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_common.worker import SessionReaper
from vai_contracts.authoring import Lexicon
from vai_stt_core.adapters import create_stt
from vai_stt_core.lexicon import LexiconCache
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

    lexicon_enabled: bool = True
    """커스텀 사전 교정 사용 여부. 사전이 배포되지 않았으면 자동으로 무시된다."""

    lexicon_ttl_s: float = 30.0


def create_app() -> FastAPI:
    common = get_settings()
    stt_cfg = SttSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        adapter = create_stt(stt_cfg.adapter)
        await adapter.initialize(stt_cfg.model_path, json.loads(stt_cfg.adapter_config))
        log.info("STT 어댑터 로드", extra={"adapter": adapter.name})

        lexicons: LexiconCache | None = None
        config_store: ConfigStore | None = None
        if stt_cfg.lexicon_enabled:
            config_store = (
                RedisConfigStore(bus.redis)
                if isinstance(bus, RedisEventBus)
                else InMemoryConfigStore()
            )
            lexicons = LexiconCache(
                CachedConfig(config_store, ConfigKind.LEXICON, Lexicon, ttl_s=stt_cfg.lexicon_ttl_s)
            )

        workers = [
            SttWorker(
                bus,
                adapter,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                publish_ui=stt_cfg.publish_ui,
                lexicons=lexicons,
            )
            for i in range(max(1, stt_cfg.workers))
        ]
        application.state.workers = workers
        reaper = SessionReaper(bus, list(workers), group=f"{common.consumer_group}:stt-reaper")
        tasks = [
            asyncio.create_task(w.run(), name=f"stt-worker-{i}") for i, w in enumerate(workers)
        ]
        tasks.append(asyncio.create_task(reaper.run(), name="stt-reaper"))
        if lexicons is not None and config_store is not None:
            # 배포 알림을 받으면 캐시 TTL을 기다리지 않고 즉시 새 사전을 쓴다.
            tasks.append(
                asyncio.create_task(
                    _watch_lexicon_updates(config_store, lexicons), name="stt-lexicon-watch"
                )
            )
        try:
            yield
        finally:
            reaper.stop()
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


async def _watch_lexicon_updates(store: ConfigStore, lexicons: LexiconCache) -> None:
    """사전 배포 알림 구독.

    알림을 놓쳐도 캐시 TTL이 만료되면 따라잡으므로, 이 태스크가 죽어도
    동작이 깨지지는 않는다 — 반영이 늦어질 뿐이다.
    """
    try:
        async for kind, tenant_id in store.watch():
            if kind is ConfigKind.LEXICON:
                lexicons.invalidate(tenant_id)
                log.info("사전 갱신 알림 수신", extra={"tenant_id": tenant_id})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("사전 알림 구독 중단 — TTL 갱신으로 계속 동작한다")


app = create_app()


def main() -> None:
    serve(app, default_port=8083)


if __name__ == "__main__":
    main()
