"""문서 색인 파이프라인 — 파싱 → 청킹 → 임베딩 → 저장.

문서 하나가 수백 청크가 되고 임베딩은 GPU를 점유하므로, 업로드 요청을
동기로 처리하면 관리자 콘솔이 멈춘다. 업로드는 즉시 접수만 하고 실제 색인은
백그라운드에서 돌린다(사양서의 비동기 인덱싱 job).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable

from redis.asyncio import Redis

from vai_contracts.retrieval import Chunk, Document, DocumentStatus
from vai_retrieval.chunking import ChunkingConfig, split_text

log = logging.getLogger(__name__)

DOC_KEY_PREFIX = "vai:kb:doc:"
DOC_INDEX_PREFIX = "vai:kb:index:"

Indexer = Callable[[list[Chunk]], "asyncio.Future[None] | asyncio.Task[None]"]


class DocumentStore(ABC):
    """문서 메타데이터 저장소.

    청크 본문은 벡터 저장소가 갖고, 여기는 색인 상태만 추적한다. 관리자가
    "왜 검색이 안 되지"라고 물을 때 답할 수 있어야 한다.

    **재기동을 넘어 남아야 한다.** 메타데이터를 잃으면 문서 목록이 비는 데
    그치지 않는다 — 벡터 저장소에는 청크가 그대로 남아 검색은 계속 그 문서를
    근거로 답하는데, 파기 요청은 "그런 문서 없습니다"로 거절된다. 개인정보
    파기 의무를 지킬 수 없는 상태가 조용히 만들어진다(docs/05 §2.2).
    """

    @abstractmethod
    async def put(self, document: Document) -> None: ...

    @abstractmethod
    async def get(self, doc_id: str) -> Document | None: ...

    @abstractmethod
    async def list_for(self, tenant_id: str, kb_id: str) -> list[Document]: ...

    @abstractmethod
    async def delete(self, doc_id: str) -> Document | None: ...


class InMemoryDocumentStore(DocumentStore):
    """테스트·단일 프로세스용. 재기동하면 사라진다."""

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}

    async def put(self, document: Document) -> None:
        self._docs[document.doc_id] = document

    async def get(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    async def list_for(self, tenant_id: str, kb_id: str) -> list[Document]:
        return [
            doc for doc in self._docs.values() if doc.tenant_id == tenant_id and doc.kb_id == kb_id
        ]

    async def delete(self, doc_id: str) -> Document | None:
        return self._docs.pop(doc_id, None)


class RedisDocumentStore(DocumentStore):
    """재기동과 복제를 넘어 남는다.

    TTL을 걸지 않는다. 색인된 문서는 지워질 때까지 존재해야 하고, 조용히
    사라지면 그 문서는 검색에는 잡히는데 관리 화면에서는 안 보이게 된다.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, doc_id: str) -> str:
        return f"{DOC_KEY_PREFIX}{doc_id}"

    def _index_key(self, tenant_id: str, kb_id: str) -> str:
        return f"{DOC_INDEX_PREFIX}{tenant_id}:{kb_id}"

    async def put(self, document: Document) -> None:
        pipe = self._redis.pipeline()
        pipe.set(self._key(document.doc_id), document.model_dump_json().encode())
        pipe.sadd(self._index_key(document.tenant_id, document.kb_id), document.doc_id)
        await pipe.execute()

    async def get(self, doc_id: str) -> Document | None:
        raw = await self._redis.get(self._key(doc_id))
        return Document.model_validate_json(raw) if raw else None

    async def list_for(self, tenant_id: str, kb_id: str) -> list[Document]:
        doc_ids = await self._redis.smembers(self._index_key(tenant_id, kb_id))
        found: list[Document] = []
        for raw_id in sorted(doc_ids):
            doc_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            document = await self.get(doc_id)
            if document is not None:
                found.append(document)
        return found

    async def delete(self, doc_id: str) -> Document | None:
        document = await self.get(doc_id)
        if document is None:
            return None
        pipe = self._redis.pipeline()
        pipe.delete(self._key(doc_id))
        pipe.srem(self._index_key(document.tenant_id, document.kb_id), doc_id)
        await pipe.execute()
        return document


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
        await self._documents.put(document)

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

        await self._documents.put(document)
        return document
