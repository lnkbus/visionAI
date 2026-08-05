"""LLM-SUM 진입점 — 워커 2개 + 요약 조회 API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.bus import EventBus, RedisEventBus, build_bus
from vai_common.resource import (
    BusyRegistry,
    MemoryBusyRegistry,
    RedisBusyRegistry,
    Yield,
)
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction
from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_contracts.summary import Summary
from vai_llm_sum.filestore import FileSummaryStore
from vai_llm_sum.ports import SummaryStore
from vai_llm_sum.store import InMemorySummaryStore, RedisSummaryStore
from vai_llm_sum.worker import (
    BLOCK_ID,
    SpeakerLabelCollector,
    SummaryWorker,
    TranscriptBuffer,
    TranscriptCollector,
)

log = logging.getLogger(__name__)


class SumSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_SUM_", extra="ignore")

    llm_url: str = "http://localhost:8085"
    timeout_s: float = 60.0
    """요약은 배치라 넉넉히 준다. 실시간 경로의 0.5초와는 성격이 다르다."""

    data_dir: str = "/var/lib/visionai/summaries"
    """요약 영속 디렉토리. 회의록은 그 자체가 산출물이라 사라지면 안 된다."""

    store: str = "file"
    """``file``(기본) 또는 ``redis``.

    Redis는 요약을 상담 이력 시스템으로 곧바로 넘기는 구성에서만 쓴다 —
    7일 TTL이 붙어 있고, 그건 보관 기간이 아니라 이관 유예다."""

    retention_days: int = 0
    """0이면 자동 파기하지 않는다. 보관기간은 고객사 정책이므로 제품이 임의로
    정하지 않되, 정하지 않았다는 사실은 기동 로그에 남긴다."""

    purge_interval_s: float = 60 * 60 * 6

    yield_to_stt: bool = True
    """STT가 인식 중이면 요약을 미룬다.

    한 장비에 둘을 올리면 메모리를 다투고, 그때 먼저 무너지는 것이 실시간
    자막이다. 회의록이 3분 늦는 것과 회의 중 자막이 끊기는 것은 비용이 다르다.
    GPU가 넉넉한 서버에서는 꺼서 병렬로 돌린다."""

    yield_max_wait_s: float = 180.0
    """양보 상한. 넘으면 포기하고 진행한다 — 늦은 회의록이 없는 회의록보다 낫다.

    통화가 끊이지 않는 상담센터에서는 "STT가 한가해질 때"가 영영 오지 않는다."""


class LlmSumClient:
    """LLM-GW 호출. 요약 프로파일(대형 모델)을 쓴다."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def __call__(self, *, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        response = await self._client.post(
            "/internal/v1/complete",
            json=CompletionRequest(
                tenant_id=tenant_id,
                prompt=prompt,
                system=system,
                profile="default",
                max_tokens=max_tokens,
            ).model_dump(mode="json"),
        )
        response.raise_for_status()
        return CompletionResponse.model_validate(response.json()).text

    async def aclose(self) -> None:
        await self._client.aclose()


def _build_store(cfg: SumSettings, bus: EventBus) -> SummaryStore:
    """저장소 선택. 기본은 파일이다 — 재기동으로 회의록이 사라지면 안 된다."""
    if cfg.store == "redis":
        if isinstance(bus, RedisEventBus):
            return RedisSummaryStore(bus.redis)
        log.warning("store=redis인데 Redis 버스가 아니다 — 인메모리로 내려간다")
        return InMemorySummaryStore()
    return FileSummaryStore(Path(cfg.data_dir), retention_days=cfg.retention_days)


async def _purge_loop(store: FileSummaryStore, interval_s: float) -> None:
    """보관기간 만료분을 주기적으로 지운다.

    기동 시 한 번만 돌리면 오래 떠 있는 서버에서는 영원히 안 지워진다.
    """
    while True:
        with contextlib.suppress(OSError):
            await store.purge_expired()
        await asyncio.sleep(interval_s)


def create_app() -> FastAPI:
    common = get_settings()
    cfg = SumSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        store = _build_store(cfg, bus)
        buffer = TranscriptBuffer()
        llm = LlmSumClient(cfg.llm_url, cfg.timeout_s)

        collector = TranscriptCollector(
            bus, buffer, group=f"{common.consumer_group}:transcript", consumer=common.consumer_name
        )
        busy: BusyRegistry = (
            RedisBusyRegistry(bus.redis) if isinstance(bus, RedisEventBus) else MemoryBusyRegistry()
        )
        summarizer = SummaryWorker(
            bus,
            buffer,
            llm,
            store,
            group=f"{common.consumer_group}:summary",
            consumer=common.consumer_name,
            yield_to_stt=Yield(busy, enabled=cfg.yield_to_stt, max_wait_s=cfg.yield_max_wait_s),
        )
        application.state.store = store
        application.state.bus = bus
        # 회의 프로파일에서 화자 라벨을 녹취록에 붙인다. 라벨이 없어도
        # (AICC이거나 SPK-DIA 미배포) 요약은 채널 이름으로 만들어진다.
        labels = SpeakerLabelCollector(
            bus, buffer, group=f"{common.consumer_group}:labels", consumer=common.consumer_name
        )
        tasks = [
            asyncio.create_task(collector.run(), name="sum-collector"),
            asyncio.create_task(labels.run(), name="sum-labels"),
            asyncio.create_task(summarizer.run(), name="sum-worker"),
        ]
        if isinstance(store, FileSummaryStore):
            log.info(
                "요약 영속 저장",
                extra={
                    "data_dir": str(store.root),
                    "retention_days": store.retention_days or "무기한",
                },
            )
            if store.retention_days > 0:
                tasks.append(
                    asyncio.create_task(_purge_loop(store, cfg.purge_interval_s), name="sum-purge")
                )
        try:
            yield
        finally:
            collector.stop()
            labels.stop()
            summarizer.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await llm.aclose()
            await bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI LLM-SUM", settings=common, lifespan=lifespan
    )

    @app.get("/internal/v1/summaries/{session_id}", response_model=Summary, tags=["summary"])
    async def get_summary(session_id: str, request: Request) -> Summary:
        summary: Summary | None = await request.app.state.store.get(session_id)
        if summary is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약을 찾을 수 없다")
        return summary

    @app.get("/internal/v1/summaries", response_model=list[Summary], tags=["summary"])
    async def list_summaries(tenant_id: str, request: Request, limit: int = 50) -> list[Summary]:
        summaries: list[Summary] = await request.app.state.store.list_for(tenant_id, limit)
        return summaries

    @app.delete("/internal/v1/summaries/{session_id}", tags=["summary"])
    async def delete_summary(session_id: str, request: Request) -> dict[str, bool]:
        """요약 파기.

        개인정보 파기 요구는 거절할 수 없다(docs/05 §2.2). 파기 자체를 감사에
        남기는 이유는, 지운 사실이 기록되지 않으면 "지웠다"를 증명할 방법도
        "누가 지웠나"를 따질 방법도 없기 때문이다.
        """
        existing: Summary | None = await request.app.state.store.get(session_id)
        removed: bool = await request.app.state.store.delete(session_id)
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약을 찾을 수 없다")
        await audit.emit(
            request.app.state.bus,
            action=AuditAction.KB_DELETE,
            actor=BLOCK_ID,
            block_id=BLOCK_ID,
            resource=f"summary:{session_id}",
            session_id=session_id,
            tenant_id=existing.tenant_id if existing else "",
            detail={"kind": "summary"},
        )
        return {"deleted": True}

    @app.put("/internal/v1/summaries/{session_id}", response_model=Summary, tags=["summary"])
    async def update_summary(session_id: str, payload: Summary, request: Request) -> Summary:
        """사람이 수정한 요약을 저장한다.

        ``edited`` 플래그가 곧 품질 지표다 — 수정률이 높으면 프롬프트나 모델을
        손봐야 한다는 신호다(docs/06 §5).
        """
        existing: Summary | None = await request.app.state.store.get(session_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약을 찾을 수 없다")
        payload.session_id = session_id
        payload.tenant_id = existing.tenant_id
        payload.edited = True
        await request.app.state.store.put(payload)
        return payload

    return app


app = create_app()


def main() -> None:
    serve(app, default_port=8089)


if __name__ == "__main__":
    main()
