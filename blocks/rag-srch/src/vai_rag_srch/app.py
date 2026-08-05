"""RAG-SRCH 내부 API — 하이브리드 검색 조회 경로.

색인(RAG-KB)과 조회(RAG-SRCH)를 별도 블록으로 나눈 이유는 부하 특성이 다르기
때문이다. 색인은 배치성이라 GPU를 길게 물고, 조회는 100ms 예산의 실시간 경로다.
같은 프로세스에 두면 대량 문서 업로드 한 번이 상담 중 팝업을 통째로 밀어낸다.

두 블록은 서로를 import 하지 않고 ``vai_retrieval`` 라이브러리와 같은 저장소를
공유한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.retrieval import SearchRequest, SearchResponse
from vai_retrieval.embedding import create_embedder
from vai_retrieval.hybrid import HybridSearchEngine, SearchBudget
from vai_retrieval.rerank import create_reranker
from vai_retrieval.store import create_store

log = logging.getLogger(__name__)
BLOCK_ID = "RAG-SRCH"


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_SRCH_", extra="ignore")

    embedder: str = "hashing"
    embedder_model: str = ""
    embedder_config: str = "{}"
    store: str = "memory"
    store_config: str = "{}"
    reranker: str = "lexical"
    reranker_model: str = ""
    warm_tenants: str = ""
    """기동 시 BM25 색인을 복원할 ``tenant:kb`` 목록(쉼표 구분).

    희소 색인은 메모리 상주라 재시작하면 사라진다. 복원하지 않으면 검색이
    밀집 축만으로 도는 '절반만 동작하는' 상태가 되는데, 이건 장애로 보이지
    않아서 더 위험하다."""


class RebuildResponse(BaseModel):
    tenant_id: str
    kb_id: str
    indexed_chunks: int


def create_app(engine: HybridSearchEngine | None = None) -> FastAPI:
    common = get_settings()
    cfg = SearchSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        search = engine
        if search is None:
            embedder = create_embedder(cfg.embedder)
            await embedder.initialize(cfg.embedder_model, json.loads(cfg.embedder_config))
            store = create_store(cfg.store)
            store_config = json.loads(cfg.store_config)
            store_config.setdefault("dimension", embedder.dimension)
            await store.initialize(store_config)
            reranker = create_reranker(cfg.reranker)
            await reranker.initialize(cfg.reranker_model, {})
            search = HybridSearchEngine(embedder, store, reranker, budget=SearchBudget())
        application.state.engine = search

        for spec in filter(None, (s.strip() for s in cfg.warm_tenants.split(","))):
            tenant_id, _, kb_id = spec.partition(":")
            count = await search.rebuild_sparse(tenant_id, kb_id)
            log.info(
                "희소 색인 복원",
                extra={"tenant_id": tenant_id, "kb_id": kb_id, "chunk_count": count},
            )
        yield

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI RAG-SRCH", settings=common, lifespan=lifespan
    )

    @app.post("/internal/v1/search", response_model=SearchResponse, tags=["search"])
    async def search(payload: SearchRequest, request: Request) -> SearchResponse:
        """하이브리드 검색 + 리랭킹. 사양서 §3 모듈 3의 2·3단계."""
        engine_: HybridSearchEngine = request.app.state.engine
        return await engine_.search(payload)

    @app.post("/internal/v1/kb/{kb_id}/rebuild", response_model=RebuildResponse, tags=["search"])
    async def rebuild(kb_id: str, tenant_id: str, request: Request) -> RebuildResponse:
        """희소 색인 재구성. 벡터 저장소를 기준으로 되살린다."""
        engine_: HybridSearchEngine = request.app.state.engine
        count = await engine_.rebuild_sparse(tenant_id, kb_id)
        return RebuildResponse(tenant_id=tenant_id, kb_id=kb_id, indexed_chunks=count)

    return app
