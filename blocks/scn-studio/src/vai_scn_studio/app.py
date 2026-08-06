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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.bus import RedisEventBus, build_bus
from vai_common.config_store import ConfigKind, ConfigStore, InMemoryConfigStore, RedisConfigStore
from vai_common.service import create_block_app, set_landing
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
    TtsLexicon,
)
from vai_contracts.dialog import Scenario, ScenarioRevision, ScenarioStage
from vai_contracts.retrieval import SearchRequest, SearchResponse
from vai_scn_studio.feedback import FeedbackStore, InMemoryFeedbackStore, RedisFeedbackStore
from vai_scn_studio.lifecycle import (
    InMemoryScenarioStore,
    RedisScenarioStore,
    ScenarioStore,
    StageError,
    blank_scenario,
    check_promotion,
    clone_flow,
    export_bundle,
    import_bundle,
    intents_from_csv,
    intents_to_csv,
    touch_draft,
)
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
    tts_url: str = "http://localhost:8098"
    """TTS-CORE — 읽기 미리듣기가 여기로 간다."""
    bot_url: str = "http://localhost:8099"
    """BOT-VOICE — 대화 시뮬레이터가 여기로 간다."""
    """FLT-MICRO. 룰 테스트를 실제 필터 구현으로 돌리기 위해 호출한다."""

    console_enabled: bool = True
    """저작 콘솔 화면 노출. 편집자 시트 라이선스로 게이팅되는 대상이다."""


class PublishResult(BaseModel):
    ruleset: RuleSet
    exported: str
    """FLT-MICRO가 읽는 형식.

    배포는 설정 채널로 자동 반영된다. 이 문자열은 채널을 쓰지 않는 구성
    (파일 주입)이나 형상 검토·백업용으로 남긴다."""


def create_app(
    store: AuthoringStore | None = None,
    feedback: FeedbackStore | None = None,
    search_client: httpx.AsyncClient | None = None,
    filter_client: httpx.AsyncClient | None = None,
    configs: ConfigStore | None = None,
    scenarios: ScenarioStore | None = None,
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
            application.state.scenarios = scenarios or InMemoryScenarioStore()
            application.state.bus = None
        else:
            bus = build_bus(common.redis_url)
            application.state.bus = bus
            if isinstance(bus, RedisEventBus):
                application.state.store = RedisAuthoringStore(bus.redis)
                application.state.feedback = RedisFeedbackStore(bus.redis)
                application.state.configs = RedisConfigStore(bus.redis)
                application.state.scenarios = RedisScenarioStore(bus.redis)
            else:
                application.state.store = InMemoryAuthoringStore()
                application.state.feedback = InMemoryFeedbackStore()
                application.state.configs = InMemoryConfigStore()
                application.state.scenarios = InMemoryScenarioStore()

        application.state.search = search_client or httpx.AsyncClient(
            base_url=cfg.search_url.rstrip("/"), timeout=10.0
        )
        application.state.filter = filter_client or httpx.AsyncClient(
            base_url=cfg.filter_url.rstrip("/"), timeout=5.0
        )
        # 저작 화면이 중계할 블록들. 기존 클라이언트와 같은 방식으로 둔다 —
        # 요청마다 만들면 연결이 매번 새로 열리고, 시뮬레이터를 연타할 때 그대로 드러난다.
        application.state.bot = httpx.AsyncClient(base_url=cfg.bot_url.rstrip("/"), timeout=15.0)
        application.state.tts = httpx.AsyncClient(base_url=cfg.tts_url.rstrip("/"), timeout=15.0)
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
        # 배포 채널로 흘린다. 이 한 줄이 없으면 "배포"는 운영자가 JSON을 파일에
        # 붙여 넣고 블록을 재기동하는 일이 된다 — 저작 도구를 만든 이유가 사라진다.
        await request.app.state.configs.publish(ConfigKind.RULESET, tenant_id, published)
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

    # ── TTS 읽기 사전 ────────────────────────────────────────────────────────
    #
    # 전처리 규칙은 숫자·날짜·단위처럼 **규칙으로 정해지는 것**을 다룬다.
    # 고유명사와 사내 용어는 규칙이 없다 — 그걸 코드로 받으면 안내 문구 하나에
    # 공급사 배포가 필요해진다.

    @app.get("/internal/v1/tts-lexicon/{tenant_id}", response_model=TtsLexicon, tags=["lexicon"])
    async def get_tts_lexicon(tenant_id: str, request: Request) -> TtsLexicon:
        found = await request.app.state.configs.load(ConfigKind.TTS_LEXICON, tenant_id, TtsLexicon)
        return found or TtsLexicon(tenant_id=tenant_id)

    @app.put("/internal/v1/tts-lexicon/{tenant_id}", response_model=TtsLexicon, tags=["lexicon"])
    async def save_tts_lexicon(tenant_id: str, payload: TtsLexicon, request: Request) -> TtsLexicon:
        """읽기 사전 저장 + 배포.

        STT 사전과 나눠 둔다. 방향이 반대이기 때문이다 — STT는 '잘못 들린 것을
        정답 표기로', 이건 '쓰인 표기를 어떻게 소리 낼지'다. 하나로 합치면
        한쪽을 고칠 때 다른 쪽이 조용히 망가진다.

        룰셋과 달리 초안 단계를 두지 않는다. 읽기가 틀리면 즉시 들리고 즉시
        되돌릴 수 있으므로, 배포 절차를 두면 얻는 것보다 잃는 것이 많다.
        """
        payload.tenant_id = tenant_id
        current = await request.app.state.configs.load(
            ConfigKind.TTS_LEXICON, tenant_id, TtsLexicon
        )
        payload.version = (current.version + 1) if current else 1
        payload.updated_at = datetime.now(UTC)
        await request.app.state.configs.publish(ConfigKind.TTS_LEXICON, tenant_id, payload)
        return payload

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

    # ── 대화 시뮬레이터 · 읽기 미리듣기 ──────────────────────────────────────
    #
    # 둘 다 다른 블록의 API를 그대로 중계한다. 저작 콘솔이 자체 구현을 갖지
    # 않는 이유는 **시험 경로와 운영 경로가 갈리면 안 되기** 때문이다 —
    # "시뮬레이터에서는 되는데 실제로는 안 된다"가 가능해진다.

    async def _relay(request: Request, which: str, path: str, payload: Any) -> Any:
        client_: httpx.AsyncClient = getattr(request.app.state, which)
        try:
            response = await client_.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                exc.response.status_code,
                detail=f"{path} 응답 {exc.response.status_code}: {exc.response.text[:200]}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{path} 에 연결할 수 없다: {exc}",
            ) from exc
        return response.json()

    async def _put_relay(request: Request, which: str, path: str, payload: Any) -> Any:
        """배포처럼 **덮어쓰는** 중계. 실패 사유를 그대로 올려 준다 —
        "배포 실패"만 보이면 저작자가 할 수 있는 일이 없다."""
        client_: httpx.AsyncClient = getattr(request.app.state, which)
        try:
            response = await client_.put(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail: Any
            try:
                detail = exc.response.json().get("detail", exc.response.text[:200])
            except ValueError:
                detail = exc.response.text[:200]
            raise HTTPException(exc.response.status_code, detail=detail) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{path} 에 연결할 수 없다: {exc}",
            ) from exc
        return response.json()

    @app.post("/internal/v1/simulate", tags=["authoring"])
    async def simulate(payload: dict[str, Any], request: Request) -> Any:
        """대화 시뮬레이터 — 배포 없이 시나리오 흐름을 확인한다.

        고객사가 시나리오를 직접 늘리려면 "이렇게 말하면 어디로 가는지"를
        스스로 볼 수 있어야 한다. 그게 안 되면 결국 공급사에 물어보게 된다.
        """
        return await _relay(request, "bot", "/internal/v1/scenarios/dry-run", payload)

    @app.post("/internal/v1/tts/preview", tags=["authoring"])
    async def tts_preview(payload: dict[str, Any], request: Request) -> Any:
        """읽기 미리듣기 — 숫자·금액·전문용어가 어떻게 읽히는지 본다.

        합성 전에 **읽기 텍스트**를 보여 준다. 소리를 들어야만 알 수 있으면
        사전을 고칠 때마다 전체 합성을 돌려야 한다.
        """
        return await _relay(request, "tts", "/internal/v1/tts/preview", payload)

    # ── 시나리오 생애주기 · 반출입 · 학습 도구 ────────────────────────────────
    #
    # "고객사가 자체적으로 시나리오를 늘려 간다"를 떠받치는 부분이다. 안내 문구
    # 한 줄에도 공급사가 나가야 하면 출동 원가가 라이선스 수익을 잠식한다.
    #
    # **단계는 한 칸씩만 올라가고, 편집하면 DRAFT로 내려간다.** 지름길을 열면
    # 결국 모두가 그 길로 다니고, 그러면 검증과 시연은 형식이 된다.

    def _scenarios(request: Request) -> ScenarioStore:
        store_: ScenarioStore = request.app.state.scenarios
        return store_

    async def _revision(request: Request, tenant_id: str) -> ScenarioRevision:
        found = await _scenarios(request).get(tenant_id)
        if found is None:
            # 빈 시나리오로 시작한다. 404를 주면 저작 화면이 첫 진입에서
            # 오류만 보여 주고, 사용자는 무엇을 해야 할지 모른다.
            return ScenarioRevision(scenario=blank_scenario(tenant_id))
        return found

    @app.get(
        "/internal/v1/scenarios/{tenant_id}", response_model=ScenarioRevision, tags=["scenario"]
    )
    async def get_scenario(tenant_id: str, request: Request) -> ScenarioRevision:
        return await _revision(request, tenant_id)

    @app.put(
        "/internal/v1/scenarios/{tenant_id}", response_model=ScenarioRevision, tags=["scenario"]
    )
    async def save_scenario(
        tenant_id: str, payload: Scenario, request: Request, note: str = "", by: str = ""
    ) -> ScenarioRevision:
        """초안 저장. 저장하는 순간 단계는 DRAFT로 돌아간다."""
        payload.tenant_id = tenant_id
        payload.published = False
        revision = await _revision(request, tenant_id)
        revision.scenario = payload
        touch_draft(revision, note=note, by=by)
        await _scenarios(request).put(tenant_id, revision)
        return revision

    @app.post(
        "/internal/v1/scenarios/{tenant_id}/promote",
        response_model=ScenarioRevision,
        tags=["scenario"],
    )
    async def promote_scenario(
        tenant_id: str, request: Request, stage: ScenarioStage
    ) -> ScenarioRevision:
        """다음 단계로 올린다.

        * → BUILT: BOT-VOICE의 **실제 검증기**로 확인한다. 저작 콘솔이 자체
          검증을 따로 두면 "여기선 통과인데 배포에서 거부"가 생긴다.
        * → LIVE: BOT-VOICE에 실제로 배포한다. 단계 표시만 바꾸고 배포하지
          않으면 화면과 현실이 갈라진다 — 그게 이 기능의 가장 나쁜 실패다.
        """
        revision = await _revision(request, tenant_id)
        try:
            check_promotion(revision.stage, stage)
        except StageError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if stage is ScenarioStage.BUILT:
            result = await _relay(
                request,
                "bot",
                "/internal/v1/scenarios/validate",
                revision.scenario.model_dump(mode="json"),
            )
            problems = list(result.get("problems", []))
            revision.problems = problems
            if problems:
                await _scenarios(request).put(tenant_id, revision)
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail={"message": "검증을 통과하지 못했다", "problems": problems},
                )

        if stage is ScenarioStage.LIVE:
            published = await _put_relay(
                request,
                "bot",
                f"/internal/v1/scenarios/{tenant_id}",
                revision.scenario.model_dump(mode="json"),
            )
            revision.scenario = Scenario.model_validate(published)
            # 배포한 판을 이력에 남긴다. 되돌릴 곳이 없으면 롤백은 말뿐이다.
            await _scenarios(request).push_history(tenant_id, revision.model_copy(deep=True))

        revision.stage = stage
        revision.updated_at = datetime.now(UTC)
        await _scenarios(request).put(tenant_id, revision)
        return revision

    @app.get(
        "/internal/v1/scenarios/{tenant_id}/history",
        response_model=list[ScenarioRevision],
        tags=["scenario"],
    )
    async def scenario_history(tenant_id: str, request: Request) -> list[ScenarioRevision]:
        return await _scenarios(request).history(tenant_id)

    @app.post(
        "/internal/v1/scenarios/{tenant_id}/restore",
        response_model=ScenarioRevision,
        tags=["scenario"],
    )
    async def restore_scenario(tenant_id: str, request: Request, version: int) -> ScenarioRevision:
        """과거 배포본을 **초안으로** 되살린다.

        곧바로 운영에 올리지 않는다. 되돌리기도 배포이므로 같은 검증을 거쳐야
        하고, 그 사이에 바뀐 다른 것과 맞는지 확인할 기회가 필요하다.
        """
        for item in await _scenarios(request).history(tenant_id):
            if item.scenario.version == version:
                revision = await _revision(request, tenant_id)
                revision.scenario = item.scenario.model_copy(deep=True)
                touch_draft(revision, note=f"v{version} 복구")
                await _scenarios(request).put(tenant_id, revision)
                return revision
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"v{version} 이력이 없다")

    @app.get("/internal/v1/scenarios/{tenant_id}/export", tags=["scenario"])
    async def export_scenario(tenant_id: str, request: Request) -> dict[str, Any]:
        """반출. 단계는 싣지 않는다 — 반입본은 언제나 초안에서 시작한다."""
        return export_bundle(await _revision(request, tenant_id))

    @app.post(
        "/internal/v1/scenarios/{tenant_id}/import",
        response_model=ScenarioRevision,
        tags=["scenario"],
    )
    async def import_scenario(
        tenant_id: str, payload: dict[str, Any], request: Request
    ) -> ScenarioRevision:
        try:
            scenario = import_bundle(payload, tenant_id=tenant_id)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        revision = await _revision(request, tenant_id)
        revision.scenario = scenario
        touch_draft(revision, note="반입")
        await _scenarios(request).put(tenant_id, revision)
        return revision

    @app.post(
        "/internal/v1/scenarios/{tenant_id}/clone-flow",
        response_model=ScenarioRevision,
        tags=["scenario"],
    )
    async def clone_scenario_flow(
        tenant_id: str, request: Request, node_ids: list[str], suffix: str = "-copy"
    ) -> ScenarioRevision:
        """흐름 복제 — 비슷한 안내를 처음부터 다시 쓰지 않게 한다."""
        revision = await _revision(request, tenant_id)
        try:
            revision.scenario = clone_flow(revision.scenario, node_ids, suffix=suffix)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        touch_draft(revision, note=f"{', '.join(node_ids)} 복제")
        await _scenarios(request).put(tenant_id, revision)
        return revision

    @app.get("/internal/v1/scenarios/{tenant_id}/intents.csv", tags=["scenario"])
    async def export_intents(tenant_id: str, request: Request) -> PlainTextResponse:
        """학습 도구 — 인텐트를 표로 내린다. 엑셀이 그대로 연다."""
        revision = await _revision(request, tenant_id)
        return PlainTextResponse(
            intents_to_csv(revision.scenario),
            media_type="text/csv; charset=utf-8",
            headers={
                "content-disposition": f'attachment; filename="intents-{tenant_id}.csv"',
            },
        )

    @app.post(
        "/internal/v1/scenarios/{tenant_id}/intents.csv",
        response_model=ScenarioRevision,
        tags=["scenario"],
    )
    async def import_intents(
        tenant_id: str, payload: dict[str, str], request: Request
    ) -> ScenarioRevision:
        """표에서 인텐트를 되받는다. **노드는 새로 만들지 않는다** —
        오타 하나로 아무 데도 연결되지 않은 노드가 생기는 것을 막는다."""
        revision = await _revision(request, tenant_id)
        try:
            revision.scenario = intents_from_csv(payload.get("csv", ""), scenario=revision.scenario)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        touch_draft(revision, note="인텐트 일괄 반입")
        await _scenarios(request).put(tenant_id, revision)
        return revision

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

        set_landing(app, "/console")

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
