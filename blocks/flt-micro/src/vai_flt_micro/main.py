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

from vai_common.bus import build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_contracts.authoring import RuleTestRequest, RuleTestResult
from vai_flt_micro.filter import MicroComplianceFilter, load_rules
from vai_flt_micro.worker import BLOCK_ID, FilterWorker

log = logging.getLogger(__name__)


class FilterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_FLT_", extra="ignore")

    rules_path: str = ""
    """컴플라이언스 룰 JSON 경로. 테넌트별 룰셋은 CORE-ADM이 관리하고
    이 블록은 파일로 주입받는다(Wave 4에서 API 기반으로 승격)."""

    publish_ui: bool = True
    workers: int = 1


def _load_rule_file(path: str) -> list[dict[str, object]]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(raw.get("rules", raw)) if isinstance(raw, dict) else list(raw)


def create_app() -> FastAPI:
    common = get_settings()
    flt_cfg = FilterSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        rules = load_rules(_load_rule_file(flt_cfg.rules_path))
        micro = MicroComplianceFilter(rules)
        log.info("컴플라이언스 룰 로드", extra={"rule_count": micro.rule_count})

        workers = [
            FilterWorker(
                bus,
                micro,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                publish_ui=flt_cfg.publish_ui,
            )
            for i in range(max(1, flt_cfg.workers))
        ]
        application.state.workers = workers
        tasks = [
            asyncio.create_task(w.run(), name=f"flt-worker-{i}") for i, w in enumerate(workers)
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
