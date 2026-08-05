"""FLT-MICRO 진입점."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

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
from vai_contracts.authoring import RuleSet, RuleTestRequest, RuleTestResult
from vai_flt_micro.filter import MicroComplianceFilter, load_rules
from vai_flt_micro.rules import RuleSetCache
from vai_flt_micro.worker import BLOCK_ID, FilterWorker

log = logging.getLogger(__name__)


class FilterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_FLT_", extra="ignore")

    rules_path: str = ""
    """파일로 주입하는 기본 룰셋(선택).

    저작 콘솔이 배포한 테넌트 룰셋이 있으면 그쪽이 이긴다. 이 파일은 배포본이
    없는 테넌트의 기본값이자, 저작 콘솔을 쓰지 않는 구성의 유일한 경로다."""

    ruleset_enabled: bool = True
    """저작 콘솔 배포 채널 구독. 끄면 파일 주입본만 쓴다."""

    ruleset_ttl_s: float = 30.0

    publish_ui: bool = True
    workers: int = 1


def _load_rule_file(path: str) -> list[dict[str, object]]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(raw.get("rules", raw)) if isinstance(raw, dict) else list(raw)


async def _watch_ruleset_updates(store: ConfigStore, rulesets: RuleSetCache) -> None:
    """룰셋 배포 알림 구독.

    알림을 놓쳐도 캐시 TTL이 만료되면 따라잡는다 — 이 태스크가 죽어도 반영이
    늦어질 뿐 동작이 깨지지는 않는다.
    """
    try:
        async for kind, tenant_id in store.watch():
            if kind is ConfigKind.RULESET:
                rulesets.invalidate(tenant_id)
                log.info("룰셋 갱신 알림 수신", extra={"tenant_id": tenant_id})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("룰셋 알림 구독 중단 — TTL 갱신으로 계속 동작한다")


def create_app() -> FastAPI:
    common = get_settings()
    flt_cfg = FilterSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        rules = load_rules(_load_rule_file(flt_cfg.rules_path))
        micro = MicroComplianceFilter(rules)
        log.info("컴플라이언스 룰 로드", extra={"rule_count": micro.rule_count})

        rulesets: RuleSetCache | None = None
        config_store: ConfigStore | None = None
        if flt_cfg.ruleset_enabled:
            config_store = (
                RedisConfigStore(bus.redis)
                if isinstance(bus, RedisEventBus)
                else InMemoryConfigStore()
            )
            rulesets = RuleSetCache(
                CachedConfig(
                    config_store, ConfigKind.RULESET, RuleSet, ttl_s=flt_cfg.ruleset_ttl_s
                ),
                micro,
            )

        workers = [
            FilterWorker(
                bus,
                micro,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                publish_ui=flt_cfg.publish_ui,
                rulesets=rulesets,
            )
            for i in range(max(1, flt_cfg.workers))
        ]
        application.state.workers = workers
        tasks = [
            asyncio.create_task(w.run(), name=f"flt-worker-{i}") for i, w in enumerate(workers)
        ]
        if rulesets is not None and config_store is not None:
            # 배포 알림을 받으면 캐시 TTL을 기다리지 않고 즉시 새 룰을 쓴다.
            tasks.append(
                asyncio.create_task(
                    _watch_ruleset_updates(config_store, rulesets), name="flt-ruleset-watch"
                )
            )
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
            await bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI FLT-MICRO", settings=common, lifespan=lifespan
    )

    @app.post("/internal/v1/filter/test", response_model=RuleTestResult, tags=["filter"])
    async def test_filter(payload: RuleTestRequest) -> RuleTestResult:
        """저작 도구(SCN-STUDIO)가 룰을 시험하는 엔드포인트.

        저작 화면의 테스트 결과와 실제 상담에서의 동작이 다르면 도구가 아니라
        함정이 된다. 그래서 저작 도구는 자체 필터를 두지 않고 **이 블록의
        실제 구현**을 호출한다. 블록 경계를 지키면서 동작 일치도 보장하는 방법이다.
        """
        rules = (
            load_rules([r.model_dump(mode="json") for r in payload.rules])
            if payload.rules is not None
            else []
        )
        outcome = MicroComplianceFilter(rules).process_text(payload.text)
        return RuleTestResult(
            clean_text=outcome.clean_text,
            pii_masked=outcome.pii_masked,
            pii_types=outcome.pii_types,
            matched_rules=[rule.rule_id for rule in outcome.matched_rules],
            elapsed_ms=round(outcome.elapsed_ms, 3),
        )

    return app


app = create_app()


def main() -> None:
    serve(app, default_port=8084)


if __name__ == "__main__":
    main()
