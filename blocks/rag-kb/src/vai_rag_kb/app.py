"""RAG-KB 내부 API — 지식베이스 문서 수집."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.bus import build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction
from vai_contracts.retrieval import Document, DocumentStatus, IngestRequest
from vai_rag_kb.indexer import DocumentStore, IngestionPipeline, new_doc_id
from vai_retrieval.embedding import create_embedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.rerank import create_reranker
from vai_retrieval.store import create_store

log = logging.getLogger(__name__)
BLOCK_ID = "RAG-KB"


class KbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_RAG_", extra="ignore")

    embedder: str = "hashing"
    embedder_model: str = ""
    embedder_config: str = "{}"
    store: str = "memory"
    store_config: str = "{}"


class DeleteResponse(BaseModel):
    doc_id: str
    removed_chunks: int


def create_app(engine: HybridSearchEngine | None = None) -> FastAPI:
    common = get_settings()
    kb_cfg = KbSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        search = engine
        if search is None:
            embedder = create_embedder(kb_cfg.embedder)
            await embedder.initialize(kb_cfg.embedder_model, json.loads(kb_cfg.embedder_config))
            store = create_store(kb_cfg.store)
            store_config = json.loads(kb_cfg.store_config)
            store_config.setdefault("dimension", embedder.dimension)
            await store.initialize(store_config)
            # 색인만 하는 블록이지만 엔진을 통째로 쓴다 — 색인과 조회가 같은
            # 청킹·임베딩 규칙을 쓰는지 코드로 보장하기 위해서다.
            reranker = create_reranker("lexical")
            await reranker.initialize("", {})
            search = HybridSearchEngine(embedder, store, reranker)
            application.state.owns_engine = True
        application.state.engine = search
        application.state.documents = DocumentStore()
        application.state.pipeline = IngestionPipeline(
            application.state.documents, search.index_chunks
        )
        # 문서 파기는 감사 대상이다(docs/05 §2.2). "지웠다"를 증명할 수 없으면
        # 파기 완전성 요구를 충족했다고 말할 수 없다.
        application.state.bus = build_bus(common.redis_url)
        log.info("지식베이스 블록 기동", extra={"embedder": kb_cfg.embedder, "store": kb_cfg.store})
        try:
            yield
        finally:
            await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI RAG-KB", settings=common, lifespan=lifespan
    )

    @app.post(
        "/internal/v1/kb/{kb_id}/documents",
        response_model=Document,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["knowledge"],
    )
    async def ingest(
        kb_id: str,
        payload: IngestRequest,
        background: BackgroundTasks,
        request: Request,
    ) -> Document:
        """문서를 접수하고 색인은 백그라운드로 넘긴다(202 Accepted)."""
        if not payload.text.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="본문이 비어 있다")

        document = Document(
            doc_id=new_doc_id(),
            tenant_id=payload.tenant_id,
            kb_id=kb_id,
            title=payload.title,
            source=payload.source,
        )
        request.app.state.documents.put(document)
        background.add_task(request.app.state.pipeline.run, document, payload.text)
        return document

    @app.get("/internal/v1/documents/{doc_id}", response_model=Document, tags=["knowledge"])
    async def get_document(doc_id: str, request: Request) -> Document:
        """색인 진행 상태 조회. 관리자 콘솔이 폴링한다."""
        document: Document | None = request.app.state.documents.get(doc_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="문서를 찾을 수 없다")
        return document

    @app.get("/internal/v1/kb/{kb_id}/documents", response_model=list[Document], tags=["knowledge"])
    async def list_documents(kb_id: str, tenant_id: str, request: Request) -> list[Document]:
        documents: list[Document] = request.app.state.documents.list_for(tenant_id, kb_id)
        return documents

    @app.delete(
        "/internal/v1/documents/{doc_id}", response_model=DeleteResponse, tags=["knowledge"]
    )
    async def delete_document(doc_id: str, request: Request) -> DeleteResponse:
        """문서 파기. 청크와 색인까지 함께 지운다(docs/05 §2.2 파기 완전성)."""
        document = request.app.state.documents.get(doc_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="문서를 찾을 수 없다")
        removed = await request.app.state.engine.remove_document(
            document.tenant_id, document.kb_id, doc_id
        )
        request.app.state.documents.delete(doc_id)
        await audit.emit(
            request.app.state.bus,
            action=AuditAction.KB_DELETE,
            actor=BLOCK_ID,
            block_id=BLOCK_ID,
            resource=doc_id,
            tenant_id=document.tenant_id,
            detail={
                "kb_id": document.kb_id,
                "removed_chunks": str(removed),
                "title": document.title,
            },
        )
        return DeleteResponse(doc_id=doc_id, removed_chunks=removed)

    @app.post("/internal/v1/kb/{kb_id}/documents/sync", response_model=Document, tags=["knowledge"])
    async def ingest_sync(kb_id: str, payload: IngestRequest, request: Request) -> Document:
        """색인 완료까지 기다리는 동기 버전 — 테스트·마이그레이션 스크립트용."""
        document = Document(
            doc_id=new_doc_id(),
            tenant_id=payload.tenant_id,
            kb_id=kb_id,
            title=payload.title,
            source=payload.source,
        )
        result: Document = await request.app.state.pipeline.run(document, payload.text)
        if result.status is DocumentStatus.FAILED:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.error or "")
        return result

    return app
