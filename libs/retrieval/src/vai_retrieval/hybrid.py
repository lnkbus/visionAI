"""하이브리드 검색 엔진 — 사양서 §3 모듈 3의 2·3단계.

밀집(의미)과 희소(정확 토큰) 결과를 **RRF(Reciprocal Rank Fusion)**로 합친다.
점수를 직접 더하지 않는 이유: 코사인 유사도는 0~1, BM25는 상한이 없는 값이라
스케일이 달라 가중합을 하려면 코퍼스마다 정규화 계수를 다시 잡아야 한다.
RRF는 순위만 쓰므로 그런 튜닝 없이 두 축을 안정적으로 섞는다.

인덱스는 이 클래스가 소유한다. RAG-KB(색인)와 RAG-SRCH(조회)가 같은 저장소를
바라보되 서로를 import 하지 않도록, 공유 대상은 블록이 아니라 이 라이브러리다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from vai_contracts.retrieval import Chunk, ScoredChunk, SearchRequest, SearchResponse
from vai_retrieval.bm25 import BM25Index
from vai_retrieval.embedding import BaseEmbedder
from vai_retrieval.rerank import BaseReranker
from vai_retrieval.store import BaseVectorStore, collection_name

log = logging.getLogger(__name__)

RRF_K = 60
"""RRF 감쇠 상수. 원 논문 권장값으로, 상위권 순위차를 과도하게 벌리지 않는다."""


@dataclass
class SearchBudget:
    """사양서 §3의 단계별 목표 지연. 초과 시 경고를 남긴다."""

    search_ms: float = 100.0
    rerank_ms: float = 80.0


class HybridSearchEngine:
    def __init__(
        self,
        embedder: BaseEmbedder,
        store: BaseVectorStore,
        reranker: BaseReranker,
        *,
        budget: SearchBudget | None = None,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._reranker = reranker
        self._budget = budget or SearchBudget()
        # 테넌트·KB별 BM25 색인. 컬렉션이 다르면 교차 검색이 구조적으로 불가능하다.
        self._sparse: dict[str, BM25Index] = {}

    def _index(self, tenant_id: str, kb_id: str) -> BM25Index:
        return self._sparse.setdefault(collection_name(tenant_id, kb_id), BM25Index())

    async def index_chunks(self, chunks: list[Chunk]) -> None:
        """색인. RAG-KB가 호출한다."""
        if not chunks:
            return
        vectors = await self._embedder.embed([chunk.text for chunk in chunks])
        await self._store.upsert(chunks, vectors)
        sparse = self._index(chunks[0].tenant_id, chunks[0].kb_id)
        for chunk in chunks:
            # 제목을 본문 앞에 붙여 색인한다. "결제일 변경 규정" 같은 제목어가
            # 본문에 한 번도 안 나오는 문서가 흔하다.
            sparse.add(chunk.chunk_id, f"{chunk.title}\n{chunk.text}")

    async def remove_document(self, tenant_id: str, kb_id: str, doc_id: str) -> int:
        """문서 파기. 벡터와 희소 색인 양쪽에서 지운다."""
        sparse = self._index(tenant_id, kb_id)
        for chunk in await self._store.get_chunks(tenant_id, kb_id):
            if chunk.doc_id == doc_id:
                sparse.remove(chunk.chunk_id)
        return await self._store.delete_document(tenant_id, kb_id, doc_id)

    async def rebuild_sparse(self, tenant_id: str, kb_id: str) -> int:
        """벡터 저장소를 기준으로 희소 색인을 다시 만든다.

        BM25 색인은 메모리 상주라 프로세스가 재시작하면 사라진다. 기동 시
        이 메서드로 복원해야 검색이 절반만 동작하는 상태를 피한다.
        """
        chunks = await self._store.get_chunks(tenant_id, kb_id)
        index = self._index(tenant_id, kb_id)
        index.clear()
        for chunk in chunks:
            index.add(chunk.chunk_id, f"{chunk.title}\n{chunk.text}")
        return len(chunks)

    async def search(self, request: SearchRequest) -> SearchResponse:
        """RAG-SRCH가 호출하는 질의 경로."""
        started = time.perf_counter()

        dense_started = time.perf_counter()
        query_vector = await self._embedder.embed_query(request.query)
        dense_hits = await self._store.search(
            request.tenant_id, request.kb_id, query_vector, request.candidate_k
        )
        dense_ms = (time.perf_counter() - dense_started) * 1000

        sparse_started = time.perf_counter()
        sparse_hits = self._index(request.tenant_id, request.kb_id).search(
            request.query, request.candidate_k
        )
        sparse_ms = (time.perf_counter() - sparse_started) * 1000

        fused = self._fuse(dense_hits, sparse_hits)
        if not fused:
            return SearchResponse(
                latency_ms=int((time.perf_counter() - started) * 1000),
                dense_ms=int(dense_ms),
                sparse_ms=int(sparse_ms),
            )

        rerank_started = time.perf_counter()
        ranked = await self._rerank(request.query, fused)
        rerank_ms = (time.perf_counter() - rerank_started) * 1000

        hits = [hit for hit in ranked if hit.score >= request.min_score][: request.top_k]
        total_ms = (time.perf_counter() - started) * 1000
        self._warn_if_over_budget(dense_ms + sparse_ms, rerank_ms)

        return SearchResponse(
            hits=hits,
            latency_ms=int(total_ms),
            dense_ms=int(dense_ms),
            sparse_ms=int(sparse_ms),
            rerank_ms=int(rerank_ms),
        )

    def _fuse(
        self, dense: list[tuple[Chunk, float]], sparse: list[tuple[str, float]]
    ) -> list[ScoredChunk]:
        """RRF로 두 순위를 합친다."""
        by_id: dict[str, Chunk] = {chunk.chunk_id: chunk for chunk, _ in dense}
        dense_scores = {chunk.chunk_id: score for chunk, score in dense}
        sparse_scores = dict(sparse)

        fused: dict[str, float] = {}
        for rank, (chunk, _) in enumerate(dense):
            fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + 1 / (RRF_K + rank + 1)
        for rank, (chunk_id, _) in enumerate(sparse):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1 / (RRF_K + rank + 1)

        results: list[ScoredChunk] = []
        for chunk_id, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            found = by_id.get(chunk_id)
            if found is None:
                # 희소 검색에만 걸린 청크. 벡터 저장소가 본문을 갖고 있으므로
                # 여기서 못 찾으면 색인 불일치다 — 조용히 버리지 않고 남긴다.
                log.debug("희소 전용 히트를 본문 없이 건너뜀", extra={"chunk_id": chunk_id})
                continue
            results.append(
                ScoredChunk(
                    chunk=found,
                    score=score,
                    dense_score=dense_scores.get(chunk_id, 0.0),
                    sparse_score=sparse_scores.get(chunk_id, 0.0),
                )
            )
        return results

    async def _rerank(self, query: str, candidates: list[ScoredChunk]) -> list[ScoredChunk]:
        scores = await self._reranker.score(query, [hit.chunk.text for hit in candidates])
        for hit, score in zip(candidates, scores, strict=True):
            hit.rerank_score = score
            hit.score = score
        return sorted(candidates, key=lambda hit: hit.score, reverse=True)

    def _warn_if_over_budget(self, search_ms: float, rerank_ms: float) -> None:
        if search_ms > self._budget.search_ms:
            log.warning(
                "검색 지연 예산 초과",
                extra={"elapsed_ms": round(search_ms, 2), "budget_ms": self._budget.search_ms},
            )
        if rerank_ms > self._budget.rerank_ms:
            log.warning(
                "리랭킹 지연 예산 초과",
                extra={"elapsed_ms": round(rerank_ms, 2), "budget_ms": self._budget.rerank_ms},
            )
