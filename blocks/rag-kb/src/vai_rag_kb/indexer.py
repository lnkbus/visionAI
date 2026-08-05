"""문서 색인 파이프라인 — 파싱 → 청킹 → 임베딩 → 저장.

문서 하나가 수백 청크가 되고 임베딩은 GPU를 점유하므로, 업로드 요청을
동기로 처리하면 관리자 콘솔이 멈춘다. 업로드는 즉시 접수만 하고 실제 색인은
백그라운드에서 돌린다(사양서의 비동기 인덱싱 job).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from vai_contracts.retrieval import Chunk, Document, DocumentStatus
from vai_retrieval.chunking import ChunkingConfig, split_text

log = logging.getLogger(__name__)

Indexer = Callable[[list[Chunk]], "asyncio.Future[None] | asyncio.Task[None]"]


class DocumentStore:
    """문서 메타데이터 레지스트리.

    청크 본문은 벡터 저장소가 갖고, 여기는 색인 상태만 추적한다. 관리자가
    "왜 검색이 안 되지"라고 물을 때 답할 수 있어야 한다.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}

    def put(self, document: Document) -> None:
        self._docs[document.doc_id] = document

    def get(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    def list_for(self, tenant_id: str, kb_id: str) -> list[Document]:
        return [
            doc for doc in self._docs.values() if doc.tenant_id == tenant_id and doc.kb_id == kb_id
        ]

    def delete(self, doc_id: str) -> Document | None:
        return self._docs.pop(doc_id, None)


def new_doc_id() -> str:
    return f"doc_{uuid.uuid4().hex[:12]}"


def build_chunks(
    document: Document, text: str, config: ChunkingConfig | None = None
) -> list[Chunk]:
    """문서 본문을 검색 단위로 나눈다."""
    pieces = split_text(text, config)
    return [
        Chunk(
            chunk_id=f"{document.doc_id}:{ordinal}",
            doc_id=document.doc_id,
            tenant_id=document.tenant_id,
            kb_id=document.kb_id,
            title=document.title,
            text=piece,
            ordinal=ordinal,
            doc_version=document.version,
        )
        for ordinal, piece in enumerate(pieces)
    ]


class IngestionPipeline:
    """업로드된 문서를 색인 가능한 상태로 만든다."""

    def __init__(
        self,
        documents: DocumentStore,
        index_chunks: Callable[[list[Chunk]], object],
        *,
        chunking: ChunkingConfig | None = None,
    ) -> None:
        self._documents = documents
        self._index_chunks = index_chunks
        self._chunking = chunking

    async def run(self, document: Document, text: str) -> Document:
        """색인 1건. 실패해도 예외를 밖으로 던지지 않고 문서 상태에 남긴다.

        백그라운드 태스크에서 도는 코드라 예외를 던지면 아무도 못 본다.
        관리자 콘솔이 실패 사유를 볼 수 있어야 재업로드 판단이 가능하다.
        """
        document.status = DocumentStatus.INDEXING
        self._documents.put(document)

        try:
            chunks = build_chunks(document, text, self._chunking)
            if not chunks:
                raise ValueError("본문에서 색인할 내용을 찾지 못했다")
            result = self._index_chunks(chunks)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            document.status = DocumentStatus.FAILED
            document.error = str(exc)
            log.exception(
                "문서 색인 실패",
                extra={"doc_id": document.doc_id, "tenant_id": document.tenant_id},
            )
        else:
            document.status = DocumentStatus.READY
            document.chunk_count = len(chunks)
            document.error = None
            log.info(
                "문서 색인 완료",
                extra={"doc_id": document.doc_id, "chunk_count": len(chunks)},
            )

        self._documents.put(document)
        return document
