"""RAG-KB 내부 API — 지식베이스 문서 수집."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common import audit
from vai_common.bus import RedisEventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction
from vai_contracts.retrieval import Document, DocumentStatus, IngestRequest
from vai_rag_kb import parsers
from vai_rag_kb.indexer import (
    DocumentStore,
    IngestionPipeline,
    InMemoryDocumentStore,
    RedisDocumentStore,
    new_doc_id,
)
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


def create_app(
    engine: HybridSearchEngine | None = None, documents: DocumentStore | None = None
) -> FastAPI:
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
        # 문서 파기는 감사 대상이다(docs/05 §2.2). "지웠다"를 증명할 수 없으면
        # 파기 완전성 요구를 충족했다고 말할 수 없다.
        event_bus = build_bus(common.redis_url)
        application.state.bus = event_bus
        # 메타데이터가 재기동을 못 넘기면 파기 자체가 불가능해진다 — 검색은
        # 계속 그 문서로 답하는데 목록에는 없어서 지울 대상을 특정할 수 없다.
        # 주입받으면 그쪽을 쓴다: 블록 계약 검증에 Redis가 필요하면 그 테스트는
        # 인프라 상태에 따라 흔들린다.
        application.state.documents = documents or (
            RedisDocumentStore(event_bus.redis)
            if isinstance(event_bus, RedisEventBus)
            else InMemoryDocumentStore()
        )
        application.state.pipeline = IngestionPipeline(
            application.state.documents, search.index_chunks
        )
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
        await request.app.state.documents.put(document)
        background.add_task(request.app.state.pipeline.run, document, payload.text)
        return document

    @app.post(
        "/internal/v1/kb/{kb_id}/documents/upload",
        response_model=Document,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["knowledge"],
    )
    async def upload(
        kb_id: str,
        background: BackgroundTasks,
        request: Request,
        file: UploadFile = File(..., description="HWP·HWPX·PDF·DOCX·평문"),
        tenant_id: str = Form(...),
        title: str = Form(""),
        source: str = Form(""),
        parser: str = Form("", description="비우면 내용의 매직 바이트로 판별한다"),
    ) -> Document:
        """문서 파일을 올려 색인한다.

        약관·매뉴얼은 평문으로 오지 않는다. 공공은 HWP, 금융은 PDF/DOCX가
        대부분이라 이 경로가 없으면 고객사가 "문서를 넣어 보세요"의 첫 단계에서
        막힌다.

        **파싱 실패는 400으로 즉시 알린다.** 빈 텍스트로 색인을 '성공' 처리하면
        고객사는 문서를 넣었는데 검색이 안 되는 이유를 영영 모른다.
        """
        content = await file.read()
        if not content:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="빈 파일이다")

        filename = file.filename or ""
        try:
            parsed = parsers.parse(content, filename=filename, parser=parser)
        except parsers.ParseError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        document = Document(
            doc_id=new_doc_id(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=title or parsed.title or filename,
            source=source or filename,
        )
        # 온전하지 않게 읽힌 부분은 문서에 남긴다. 조용히 넘기면
        # "왜 이 조항이 검색이 안 되지"를 아무도 설명하지 못한다.
        document.warnings = list(parsed.warnings)
        await request.app.state.documents.put(document)
        background.add_task(request.app.state.pipeline.run, document, parsed.text)
        return document

    @app.get("/internal/v1/parsers", tags=["knowledge"])
    async def list_parsers() -> dict[str, list[str]]:
        """지원 형식. 고객사가 반입 전에 확인한다."""
        return {"parsers": parsers.available()}

    @app.get("/internal/v1/documents/{doc_id}", response_model=Document, tags=["knowledge"])
    async def get_document(doc_id: str, request: Request) -> Document:
        """색인 진행 상태 조회. 관리자 콘솔이 폴링한다."""
        document: Document | None = await request.app.state.documents.get(doc_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="문서를 찾을 수 없다")
        return document

    @app.get("/internal/v1/kb/{kb_id}/documents", response_model=list[Document], tags=["knowledge"])
    async def list_documents(kb_id: str, tenant_id: str, request: Request) -> list[Document]:
        documents: list[Document] = await request.app.state.documents.list_for(tenant_id, kb_id)
        return documents

    @app.delete(
        "/internal/v1/documents/{doc_id}", response_model=DeleteResponse, tags=["knowledge"]
    )
    async def delete_document(
        doc_id: str, request: Request, tenant_id: str = "", kb_id: str = ""
    ) -> DeleteResponse:
        """문서 파기. 청크와 색인까지 함께 지운다(docs/05 §2.2 파기 완전성).

        ``tenant_id``·``kb_id``를 주면 **메타데이터 없이도 지운다.** 파기 요구는
        거절할 수 없는데, 메타데이터가 있어야만 지울 수 있게 만들면 그 메타데이터를
        잃은 순간 파기가 불가능해진다 — 벡터 저장소에는 청크가 그대로 남아
        검색은 계속 그 문서로 답하는 채로.
        """
        document = await request.app.state.documents.get(doc_id)
        if document is None and not (tenant_id and kb_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail="문서를 찾을 수 없다 — tenant_id·kb_id를 주면 메타데이터 없이 지운다",
            )

        target_tenant = document.tenant_id if document else tenant_id
        target_kb = document.kb_id if document else kb_id
        removed = await request.app.state.engine.remove_document(target_tenant, target_kb, doc_id)
        await request.app.state.documents.delete(doc_id)
        await audit.emit(
            request.app.state.bus,
            action=AuditAction.KB_DELETE,
            actor=BLOCK_ID,
            block_id=BLOCK_ID,
            resource=doc_id,
            tenant_id=target_tenant,
            detail={
                "kb_id": target_kb,
                "removed_chunks": str(removed),
                "title": document.title if document else "",
                "metadata_present": str(document is not None),
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
