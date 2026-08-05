"""SCN-STUDIO — 시나리오·룰 저작 API + 학습/품질 도구.

고객사 운영자가 직접 쓰는 블록이다. 여기서 되는 일:

* **컴플라이언스 룰 저작** — 초안 편집 → 즉시 테스트 → 배포 → (문제 시) 되돌리기
* **STT 커스텀 사전** — 상품명·전문용어 등록, 오인식 표기 교정
* **검색 튜닝 콘솔** — 질의를 넣어 Dense/Sparse/리랭킹 점수를 분해해서 확인
* **평가셋 실행** — 골든셋으로 검색 품질을 측정, 변경 전후 비교
* **팝업 피드백 집계** — 상담원 채택률과 개선 대상 질의

실시간 상담 경로와 프로세스를 분리한다. 운영자가 평가셋 100건을 돌리는 동안
상담 중 팝업이 밀리면 안 된다.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.bus import RedisEventBus, build_bus
from vai_common.config_store import ConfigKind, ConfigStore, InMemoryConfigStore, RedisConfigStore
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction
from vai_contracts.authoring import (
    ComplianceRuleSpec,
    EvalCaseResult,
    EvalReport,
    EvalRequest,
    FeedbackStats,
    Lexicon,
    PopupFeedback,
    RuleSet,
    RuleTestRequest,
    RuleTestResult,
)
from vai_contracts.retrieval import SearchRequest, SearchResponse
from vai_scn_studio.feedback import FeedbackStore, InMemoryFeedbackStore, RedisFeedbackStore
from vai_scn_studio.store import (
    AuthoringStore,
    InMemoryAuthoringStore,
    RedisAuthoringStore,
    export_rules_for_pipeline,
)
from vai_scn_studio.validation import validate_pattern

log = logging.getLogger(__name__)
BLOCK_ID = "SCN-STUDIO"
STATIC_DIR = Path(__file__).parent / "static"


class StudioSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_STUDIO_", extra="ignore")

    search_url: str = "http://localhost:8087"
    kb_url: str = "http://localhost:8086"
    filter_url: str = "http://localhost:8084"
    """FLT-MICRO. 룰 테스트를 실제 필터 구현으로 돌리기 위해 호출한다."""

    console_enabled: bool = True
    """저작 콘솔 화면 노출. 편집자 시트 라이선스로 게이팅되는 대상이다."""


class PublishResult(BaseModel):
    ruleset: RuleSet
    exported: str
    """FLT-MICRO가 읽는 형식. 현재는 운영자가 이 내용을 룰 파일로 반영한다
    (Wave 6에서 API 배포로 자동화)."""


def create_app(
    store: AuthoringStore | None = None,
    feedback: FeedbackStore | None = None,
    search_client: httpx.AsyncClient | None = None,
    filter_client: httpx.AsyncClient | None = None,
    configs: ConfigStore | None = None,
) -> FastAPI:
    common = get_settings()
    cfg = StudioSettings()
    injected = store is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            application.state.store = store
            application.state.feedback = feedback or InMemoryFeedbackStore()
            application.state.configs = configs or InMemoryConfigStore()
            application.state.bus = None
        else:
            bus = build_bus(common.redis_url)
            application.state.bus = bus
            if isinstance(bus, RedisEventBus):
                application.state.store = RedisAuthoringStore(bus.redis)
                application.state.feedback = RedisFeedbackStore(bus.redis)
                application.state.configs = RedisConfigStore(bus.redis)
            else:
                application.state.store = InMemoryAuthoringStore()
                application.state.feedback = InMemoryFeedbackStore()
                application.state.configs = InMemoryConfigStore()

        application.state.search = search_client or httpx.AsyncClient(
            base_url=cfg.search_url.rstrip("/"), timeout=10.0
        )
        application.state.filter = filter_client or httpx.AsyncClient(
            base_url=cfg.filter_url.rstrip("/"), timeout=5.0
        )
        try:
            yield
        finally:
            if not injected:
                await application.state.search.aclose()
                await application.state.filter.aclose()
                if application.state.bus is not None:
                    await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI SCN-STUDIO", settings=common, lifespan=lifespan
    )

    # ── 컴플라이언스 룰 저작 ─────────────────────────────────────────────────

    @app.get("/internal/v1/rules/{tenant_id}", response_model=RuleSet, tags=["rules"])
    async def get_rules(tenant_id: str, request: Request, published: bool = False) -> RuleSet:
        found = await request.app.state.store.get_rules(tenant_id, published=published)
        return found or RuleSet(tenant_id=tenant_id)

    @app.put("/internal/v1/rules/{tenant_id}", response_model=RuleSet, tags=["rules"])
    async def save_draft(tenant_id: str, payload: RuleSet, request: Request) -> RuleSet:
        """초안 저장. 배포 전까지 상담 파이프라인에는 영향이 없다."""
        payload.tenant_id = tenant_id
        duplicates = [
            rule_id
            for rule_id, count in Counter(r.rule_id for r in payload.rules).items()
            if count > 1
        ]
        if duplicates:
            # 룰 ID가 겹치면 어느 룰이 걸렸는지 로그에서 구분할 수 없게 된다.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"룰 ID 중복: {', '.join(duplicates)}"
            )
        for rule in payload.rules:
            error = validate_pattern(rule.pattern)
            if error:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"룰 '{rule.rule_id}'의 정규식 오류: {error}",
                )
        saved: RuleSet = await request.app.state.store.save_draft_rules(payload)
        return saved

    @app.post(
        "/internal/v1/rules/{tenant_id}/publish", response_model=PublishResult, tags=["rules"]
    )
    async def publish(tenant_id: str, request: Request) -> PublishResult:
        """초안을 배포본으로 승격한다. 직전 배포본은 이력으로 남아 되돌릴 수 있다."""
        published = await request.app.state.store.publish_rules(tenant_id)
        if published is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="배포할 초안이 없다")
        log.info(
            "룰셋 배포",
            extra={
                "tenant_id": tenant_id,
                "version": published.version,
                "rule_count": len(published.rules),
            },
        )
        # 컴플라이언스 룰 배포는 상담 동작을 바꾸는 행위다. 누가 언제 어느
        # 버전을 내보냈는지가 남아야 사후에 "그때 왜 이 문구가 안 걸렸나"를 답할 수 있다.
        if request.app.state.bus is not None:
            await audit.emit(
                request.app.state.bus,
                action=AuditAction.RULE_PUBLISH,
                actor=BLOCK_ID,
                block_id=BLOCK_ID,
                resource=f"{tenant_id}:v{published.version}",
                tenant_id=tenant_id,
                detail={"version": str(published.version), "rule_count": str(len(published.rules))},
            )
        return PublishResult(ruleset=published, exported=export_rules_for_pipeline(published))

    @app.get("/internal/v1/rules/{tenant_id}/history", response_model=list[RuleSet], tags=["rules"])
    async def history(tenant_id: str, request: Request) -> list[RuleSet]:
        items: list[RuleSet] = await request.app.state.store.rule_history(tenant_id)
        return items

    @app.post("/internal/v1/rules/test", response_model=RuleTestResult, tags=["rules"])
    async def test_rules(payload: RuleTestRequest, request: Request) -> RuleTestResult:
        """저작 중 즉시 검증.

        배포한 뒤에야 오작동을 알면 이미 상담 로그가 오염된 뒤다. 문장을 넣어
        마스킹 결과와 매칭된 룰을 바로 확인할 수 있어야 한다.
        """
        rules = payload.rules
        if rules is None:
            draft = await request.app.state.store.get_rules(payload.tenant_id, published=False)
            rules = draft.rules if draft else []
        return await _test_with_filter_block(request, payload.text, rules)

    # ── STT 커스텀 사전 ──────────────────────────────────────────────────────

    @app.get("/internal/v1/lexicon/{tenant_id}", response_model=Lexicon, tags=["lexicon"])
    async def get_lexicon(tenant_id: str, request: Request) -> Lexicon:
        found = await request.app.state.store.get_lexicon(tenant_id)
        return found or Lexicon(tenant_id=tenant_id)

    @app.put("/internal/v1/lexicon/{tenant_id}", response_model=Lexicon, tags=["lexicon"])
    async def save_lexicon(tenant_id: str, payload: Lexicon, request: Request) -> Lexicon:
        """상품명·전문용어 등록.

        범용 STT는 "무배당 행복플러스보험" 같은 고유명사를 거의 확실히 틀린다.
        틀린 인식은 검색과 요약까지 함께 망가뜨리므로 사전 교정이 필요하다.
        """
        payload.tenant_id = tenant_id
        saved: Lexicon = await request.app.state.store.save_lexicon(payload)
        # 배포 채널로 내보낸다. STT-CORE가 여기서 읽어 인식 교정에 쓴다 —
        # 실시간 블록이 저작 도구를 직접 부르지 않게 하려는 것이다.
        await request.app.state.configs.publish(ConfigKind.LEXICON, tenant_id, saved)
        return saved

    # ── 검색 튜닝 · 평가 ─────────────────────────────────────────────────────

    @app.post("/internal/v1/tuning/search", response_model=SearchResponse, tags=["tuning"])
    async def tune_search(payload: SearchRequest, request: Request) -> SearchResponse:
        """검색 결과를 점수 분해와 함께 돌려준다.

        운영자가 "왜 이 문서가 안 나오지"를 스스로 진단할 수 있어야 한다.
        Dense·Sparse·리랭킹 중 어디서 밀렸는지가 개선 방향을 결정한다.
        """
        return await _search(request, payload)

    @app.post("/internal/v1/eval/run", response_model=EvalReport, tags=["tuning"])
    async def run_eval(payload: EvalRequest, request: Request) -> EvalReport:
        """골든셋으로 검색 품질을 측정한다.

        룰·사전·청킹을 바꾼 뒤 좋아졌는지 나빠졌는지를 숫자로 확인하지 못하면
        모든 튜닝이 추측이 된다.
        """
        results: list[EvalCaseResult] = []
        for case in payload.cases:
            response = await _search(
                request,
                SearchRequest(
                    tenant_id=payload.tenant_id,
                    kb_id=payload.kb_id,
                    query=case.query,
                    top_k=payload.top_k,
                ),
            )
            returned = [hit.chunk.doc_id for hit in response.hits]
            rank = next(
                (i + 1 for i, doc_id in enumerate(returned) if doc_id in case.expected_doc_ids),
                None,
            )
            results.append(
                EvalCaseResult(
                    case_id=case.case_id,
                    query=case.query,
                    hit=rank is not None,
                    rank=rank,
                    returned_doc_ids=returned,
                    latency_ms=response.latency_ms,
                )
            )

        return _build_report(results)

    # ── 팝업 피드백 ──────────────────────────────────────────────────────────

    @app.post("/internal/v1/feedback", status_code=status.HTTP_202_ACCEPTED, tags=["feedback"])
    async def record_feedback(payload: PopupFeedback, request: Request) -> dict[str, str]:
        """상담원이 팝업을 썼는지 기록한다. 품질 개선 루프의 유일한 객관 신호다."""
        await request.app.state.feedback.add(payload)
        return {"status": "accepted"}

    @app.get("/internal/v1/feedback/{tenant_id}", response_model=FeedbackStats, tags=["feedback"])
    async def feedback_stats(tenant_id: str, request: Request) -> FeedbackStats:
        stats: FeedbackStats = await request.app.state.feedback.stats(tenant_id)
        return stats

    if cfg.console_enabled:

        @app.get("/console", include_in_schema=False)
        async def console() -> FileResponse:
            return FileResponse(STATIC_DIR / "console.html")

    return app


async def _test_with_filter_block(
    request: Request, text: str, rules: list[ComplianceRuleSpec]
) -> RuleTestResult:
    """FLT-MICRO의 **실제 구현**으로 테스트한다.

    저작 화면의 테스트 결과와 상담에서의 실제 동작이 다르면 도구가 아니라
    함정이 된다. 자체 필터를 두는 대신 필터 블록을 HTTP로 호출해, 블록 경계를
    지키면서 동작 일치도 보장한다.
    """
    client: httpx.AsyncClient = request.app.state.filter
    body = RuleTestRequest(tenant_id="", text=text, rules=rules)
    try:
        response = await client.post("/internal/v1/filter/test", json=body.model_dump(mode="json"))
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="필터 블록에 연결할 수 없다"
        ) from exc
    return RuleTestResult.model_validate(response.json())


async def _search(request: Request, payload: SearchRequest) -> SearchResponse:
    client: httpx.AsyncClient = request.app.state.search
    try:
        response = await client.post("/internal/v1/search", json=payload.model_dump(mode="json"))
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="검색 서비스에 연결할 수 없다"
        ) from exc
    return SearchResponse.model_validate(response.json())


def _build_report(results: list[EvalCaseResult]) -> EvalReport:
    if not results:
        return EvalReport()
    latencies = sorted(r.latency_ms for r in results)
    hits = sum(1 for r in results if r.hit)
    # p95는 표본이 적을 때 마지막 값에 수렴한다. 20건 미만이면 참고치일 뿐이다.
    p95_index = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95) - 1))
    return EvalReport(
        total=len(results),
        hits=hits,
        recall_at_k=round(hits / len(results), 4),
        mean_latency_ms=round(statistics.fmean(latencies), 2),
        p95_latency_ms=float(latencies[p95_index]),
        results=results,
    )
