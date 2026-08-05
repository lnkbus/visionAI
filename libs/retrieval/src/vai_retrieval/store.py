"""벡터 저장소 어댑터.

사양서는 Qdrant를 지정한다. 다만 CI·소형 PoC까지 Qdrant를 요구하면 개발
루프가 무거워지므로, 같은 인터페이스의 메모리 구현을 함께 둔다. 인터페이스가
같으니 블록 코드는 어느 쪽이 붙었는지 모른다.

테넌트 격리는 **컬렉션 분리**로 한다. 필터 조건으로만 나누면 질의 하나를
잘못 짜는 순간 교차 테넌트 유출이 되지만, 컬렉션이 다르면 구조적으로 불가능하다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import numpy as np

from vai_contracts.retrieval import Chunk

log = logging.getLogger(__name__)


def collection_name(tenant_id: str, kb_id: str) -> str:
    return f"vai_{tenant_id}_{kb_id}"


class BaseVectorStore(ABC):
    name: str

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...

    @abstractmethod
    async def search(
        self, tenant_id: str, kb_id: str, vector: np.ndarray, top_k: int
    ) -> list[tuple[Chunk, float]]:
        """코사인 유사도 상위 ``top_k``를 (청크, 점수)로 돌려준다."""

    @abstractmethod
    async def delete_document(self, tenant_id: str, kb_id: str, doc_id: str) -> int:
        """문서에 속한 청크를 모두 지우고 지운 개수를 돌려준다.

        원본 문서 삭제 시 청크가 남으면 파기 의무를 못 지킨다(docs/05 §2.2).
        """

    @abstractmethod
    async def get_chunks(self, tenant_id: str, kb_id: str) -> list[Chunk]: ...

    @abstractmethod
    async def get_by_ids(self, tenant_id: str, kb_id: str, chunk_ids: list[str]) -> list[Chunk]:
        """청크 ID로 본문을 가져온다.

        하이브리드 융합에 필요하다. 희소(BM25) 검색은 청크 ID만 돌려주는데,
        그중 밀집 상위권에 없던 것은 본문을 따로 가져와야 결과에 실을 수 있다.
        이 경로가 없으면 **키워드로만 걸린 문서가 조용히 버려진다** — 그러면
        하이브리드라고 부를 이유가 없다.
        """

    async def close(self) -> None:
        return None


class MemoryVectorStore(BaseVectorStore):
    """넘파이 완전 탐색. 수만 청크까지는 실측 수 ms로 충분하다.

    **단일 프로세스 전용이다.** RAG-KB와 RAG-SRCH를 별도 컨테이너로 띄우면서
    이걸 쓰면, 색인한 문서가 검색 쪽에 보이지 않는다. 그런데 오류가 나지 않고
    "검색 결과 없음"으로만 보여서 원인을 찾기 어렵다 — 그래서 기동 시 경고한다.
    """

    name = "memory"

    def __init__(self) -> None:
        # 컬렉션 → 청크 ID → (청크, 벡터). 삽입 순서가 곧 조회 순서다.
        self._data: dict[str, dict[str, tuple[Chunk, np.ndarray]]] = {}

    async def initialize(self, config: dict[str, Any]) -> None:
        if config.get("multiprocess_warning", True):
            log.warning(
                "메모리 벡터 저장소는 단일 프로세스 전용이다 — 색인과 조회가 다른 "
                "컨테이너라면 검색 결과가 항상 비게 된다. 다중 컨테이너 배포에는 "
                "qdrant 어댑터를 쓴다."
            )

    async def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if not chunks:
            return
        key = collection_name(chunks[0].tenant_id, chunks[0].kb_id)
        collection = self._data.setdefault(key, {})
        for chunk, vector in zip(chunks, vectors, strict=True):
            collection[chunk.chunk_id] = (chunk, np.asarray(vector, dtype=np.float32))

    async def search(
        self, tenant_id: str, kb_id: str, vector: np.ndarray, top_k: int
    ) -> list[tuple[Chunk, float]]:
        collection = self._data.get(collection_name(tenant_id, kb_id))
        if not collection:
            return []
        entries = list(collection.values())
        matrix = np.stack([entry[1] for entry in entries])
        # 저장 시점에 정규화되어 있으므로 내적이 곧 코사인 유사도다.
        scores = matrix @ np.asarray(vector, dtype=np.float32)
        top = np.argsort(scores)[::-1][:top_k]
        return [(entries[i][0], float(scores[i])) for i in top]

    async def delete_document(self, tenant_id: str, kb_id: str, doc_id: str) -> int:
        collection = self._data.get(collection_name(tenant_id, kb_id))
        if not collection:
            return 0
        doomed = [cid for cid, (chunk, _) in collection.items() if chunk.doc_id == doc_id]
        for chunk_id in doomed:
            del collection[chunk_id]
        return len(doomed)

    async def get_chunks(self, tenant_id: str, kb_id: str) -> list[Chunk]:
        collection = self._data.get(collection_name(tenant_id, kb_id), {})
        return [chunk for chunk, _ in collection.values()]

    async def get_by_ids(self, tenant_id: str, kb_id: str, chunk_ids: list[str]) -> list[Chunk]:
        collection = self._data.get(collection_name(tenant_id, kb_id), {})
        found = [collection.get(chunk_id) for chunk_id in chunk_ids]
        return [entry[0] for entry in found if entry is not None]


class QdrantVectorStore(BaseVectorStore):
    """사양서 지정 벡터 DB. 폐쇄망에는 컨테이너로 함께 반입한다."""

    name = "qdrant"

    def __init__(self) -> None:
        self._client: Any = None
        self._dimension = 0

    async def initialize(self, config: dict[str, Any]) -> None:
        from qdrant_client import AsyncQdrantClient

        self._dimension = int(config.get("dimension", 512))
        self._client = AsyncQdrantClient(
            url=config.get("url", "http://localhost:6333"), api_key=config.get("api_key")
        )

    async def _ensure_collection(self, key: str) -> None:
        from qdrant_client.models import Distance, VectorParams

        if await self._client.collection_exists(key):
            return
        await self._client.create_collection(
            key, vectors_config=VectorParams(size=self._dimension, distance=Distance.COSINE)
        )

    async def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        from qdrant_client.models import PointStruct

        if not chunks:
            return
        key = collection_name(chunks[0].tenant_id, chunks[0].kb_id)
        await self._ensure_collection(key)
        await self._client.upsert(
            key,
            points=[
                PointStruct(
                    id=_point_id(chunk.chunk_id),
                    vector=vector.tolist(),
                    payload=chunk.model_dump(mode="json"),
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )

    async def search(
        self, tenant_id: str, kb_id: str, vector: np.ndarray, top_k: int
    ) -> list[tuple[Chunk, float]]:
        key = collection_name(tenant_id, kb_id)
        if not await self._client.collection_exists(key):
            return []
        found = await self._client.query_points(key, query=vector.tolist(), limit=top_k)
        return [(Chunk.model_validate(p.payload), float(p.score)) for p in found.points]

    async def delete_document(self, tenant_id: str, kb_id: str, doc_id: str) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        key = collection_name(tenant_id, kb_id)
        if not await self._client.collection_exists(key):
            return 0
        condition = Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])
        before = (await self._client.count(key, count_filter=condition)).count
        await self._client.delete(key, points_selector=condition)
        return int(before)

    async def get_chunks(self, tenant_id: str, kb_id: str) -> list[Chunk]:
        key = collection_name(tenant_id, kb_id)
        if not await self._client.collection_exists(key):
            return []
        points, _ = await self._client.scroll(key, limit=10_000, with_payload=True)
        return [Chunk.model_validate(p.payload) for p in points]

    async def get_by_ids(self, tenant_id: str, kb_id: str, chunk_ids: list[str]) -> list[Chunk]:
        key = collection_name(tenant_id, kb_id)
        if not chunk_ids or not await self._client.collection_exists(key):
            return []
        points = await self._client.retrieve(
            key, ids=[_point_id(chunk_id) for chunk_id in chunk_ids], with_payload=True
        )
        return [Chunk.model_validate(point.payload) for point in points]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


def _point_id(chunk_id: str) -> str:
    """Qdrant 포인트 ID는 UUID 또는 정수만 허용한다."""
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def _load_qdrant() -> BaseVectorStore:
    return QdrantVectorStore()


_REGISTRY: dict[str, Callable[[], BaseVectorStore]] = {
    "memory": MemoryVectorStore,
    "qdrant": _load_qdrant,
}


def create_store(name: str) -> BaseVectorStore:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 벡터 저장소 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None
