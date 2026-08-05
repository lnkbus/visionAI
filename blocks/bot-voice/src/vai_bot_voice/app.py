"""BOT-VOICE — 음성봇 블록.

HTTP는 시나리오 저작·시험용이다. 상담은 버스로 흐른다.

**시나리오 배포 전 검사를 강제한다.** 막다른 골목이 있는 시나리오가 운영에
올라가면 고객이 갇히고, 그 사실은 통화가 끊긴 뒤에야 알게 된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_bot_voice import BLOCK_ID
from vai_bot_voice.classify import (
    BaseIntentClassifier,
    ChainClassifier,
    ExampleSimilarityClassifier,
    SlmIntentClassifier,
)
from vai_bot_voice.clients import LlmClient
from vai_bot_voice.engine import DialogEngine, validate
from vai_bot_voice.store import DialogStore, InMemoryDialogStore, RedisDialogStore
from vai_bot_voice.worker import BotWorker
from vai_common.bus import EventBus, RedisEventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.dialog import DialogState, Scenario
from vai_contracts.speech import VoiceProfile

log = logging.getLogger(__name__)


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_BOT_", extra="ignore")

    voice_id: str = "default"
    workers: int = 2

    classify_examples: bool = True
    """예시 발화 기반 어휘 분류. 모델이 필요 없어 소형 프로파일에서도 켜 둔다."""

    classify_slm: bool = False
    """sLLM 의도 분류. **어휘 분류가 기권했을 때만** 호출한다.

    기본 꺼짐이다 — 켜면 통화마다 GPU를 더 쓰고, 그 값어치는 고객사 시나리오와
    모델에 따라 다르다. 어휘 분류만으로 부족하다는 것을 평가셋으로 확인한 뒤
    켜는 것이 순서다."""

    llm_url: str = "http://localhost:8085"
    classify_profile: str = "slm"


class DryRunRequest(BaseModel):
    """저작 도구의 대화 시뮬레이터 입력."""

    scenario: Scenario
    utterances: list[str] = Field(default_factory=list)


class DryRunTurn(BaseModel):
    user: str
    bot: str
    node_id: str
    slots: dict[str, str] = Field(default_factory=dict)
    handed_off: bool = False


class DryRunResult(BaseModel):
    problems: list[str] = Field(default_factory=list)
    turns: list[DryRunTurn] = Field(default_factory=list)
    finished: bool = False


def create_app(store: DialogStore | None = None, bus: EventBus | None = None) -> FastAPI:
    common = get_settings()
    cfg = BotSettings()
    injected = bus is not None

    def build_classifier() -> tuple[BaseIntentClassifier | None, LlmClient | None]:
        """비용이 싼 단계부터 잇는다. 앞이 답하면 뒤는 호출되지 않는다."""
        stages: list[BaseIntentClassifier] = []
        if cfg.classify_examples:
            stages.append(ExampleSimilarityClassifier())
        llm: LlmClient | None = None
        if cfg.classify_slm:
            llm = LlmClient(cfg.llm_url, profile=cfg.classify_profile)
            stages.append(SlmIntentClassifier(llm.complete))
        return (ChainClassifier(*stages) if stages else None), llm

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_bus = bus or build_bus(common.redis_url)
        active_store = store
        if active_store is None:
            active_store = (
                RedisDialogStore(active_bus.redis)
                if isinstance(active_bus, RedisEventBus)
                else InMemoryDialogStore()
            )
        application.state.bus = active_bus
        application.state.store = active_store
        classifier, llm = build_classifier()

        workers = [
            BotWorker(
                active_bus,
                active_store,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                voice=VoiceProfile(voice_id=cfg.voice_id),
                classifier=classifier,
            )
            for i in range(max(1, cfg.workers))
        ]
        tasks = [asyncio.create_task(w.run(), name=f"bot-{i}") for i, w in enumerate(workers)]
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
            if llm is not None:
                await llm.aclose()
            if not injected:
                await active_bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI BOT-VOICE", settings=common, lifespan=lifespan
    )

    @app.post("/internal/v1/scenarios/validate", tags=["scenario"])
    async def check(scenario: Scenario) -> dict[str, list[str]]:
        """배포 전 검사. 저작 화면이 타이핑하는 동안 호출한다."""
        return {"problems": validate(scenario)}

    @app.put("/internal/v1/scenarios/{tenant_id}", response_model=Scenario, tags=["scenario"])
    async def publish(tenant_id: str, scenario: Scenario, request: Request) -> Scenario:
        """시나리오 배포. **문제가 있으면 거부한다.**

        막다른 골목이 있는 시나리오가 운영에 올라가면 고객이 갇히고,
        그 사실은 통화가 끊긴 뒤에야 알게 된다.
        """
        problems = validate(scenario)
        if problems:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={"message": "시나리오에 문제가 있다", "problems": problems},
            )
        scenario.tenant_id = tenant_id
        scenario.published = True
        await request.app.state.store.put_scenario(scenario)
        log.info(
            "시나리오 배포",
            extra={
                "tenant_id": tenant_id,
                "version": scenario.version,
                "nodes": len(scenario.nodes),
            },
        )
        return scenario

    @app.get("/internal/v1/scenarios/{tenant_id}", response_model=Scenario, tags=["scenario"])
    async def get_scenario(tenant_id: str, request: Request) -> Scenario:
        store_: DialogStore = request.app.state.store
        found = await store_.get_scenario(tenant_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="배포된 시나리오가 없다")
        return found

    @app.post("/internal/v1/scenarios/dry-run", response_model=DryRunResult, tags=["scenario"])
    async def dry_run(payload: DryRunRequest) -> DryRunResult:
        """대화 시뮬레이터 — 배포 없이 흐름을 확인한다.

        고객사가 시나리오를 직접 만들려면 "이렇게 말하면 어디로 가는지"를
        스스로 볼 수 있어야 한다. 그게 안 되면 결국 공급사에 물어보게 된다.
        """
        problems = validate(payload.scenario)
        engine = DialogEngine(payload.scenario)
        result = engine.start("dry-run", payload.scenario.tenant_id)

        turns = [
            DryRunTurn(
                user="",
                bot=result.text,
                node_id=result.state.current_node if result.state else "",
                slots=dict(result.state.slots) if result.state else {},
                handed_off=any(u.handoff for u in result.utterances),
            )
        ]
        state: DialogState | None = result.state
        for utterance in payload.utterances:
            if state is None or state.finished:
                break
            step = engine.reply(state, utterance)
            state = step.state
            turns.append(
                DryRunTurn(
                    user=utterance,
                    bot=step.text,
                    node_id=state.current_node if state else "",
                    slots=dict(state.slots) if state else {},
                    handed_off=any(u.handoff for u in step.utterances),
                )
            )
        return DryRunResult(problems=problems, turns=turns, finished=bool(state and state.finished))

    @app.get("/internal/v1/sessions/{session_id}/state", response_model=DialogState, tags=["bot"])
    async def state_of(session_id: str, request: Request) -> DialogState:
        store_: DialogStore = request.app.state.store
        found = await store_.get_state(session_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="대화 상태가 없다")
        return found

    return app
