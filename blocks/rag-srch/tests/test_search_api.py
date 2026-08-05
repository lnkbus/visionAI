"""RAG-SRCH 조회 API 계약 테스트."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from vai_contracts.retrieval import Chunk
from vai_rag_srch.app import create_app
from vai_retrieval.embedding import HashingEmbedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.rerank import LexicalOverlapReranker
from vai_retrieval.store import MemoryVectorStore

TENANT = "t1"
KB = "card"


@pytest.fixture
async def engine() -> HybridSearchEngine:
    embedder = HashingEmbedder()
    await embedder.initialize("", {})
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    reranker = LexicalOverlapReranker()
    await reranker.initialize("", {})
    search = HybridSearchEngine(embedder, store, reranker)
    await search.index_chunks(
        [
            Chunk(
                chunk_id="c0",
                doc_id="doc_policy",
                tenant_id=TENANT,
                kb_id=KB,
                title="신용카드 결제일 변경 및 연기 규정",
                text="단기 결제 연기는 연체 발생 전 최대 5일까지 신청 가능합니다.",
            ),
            Chunk(
                chunk_id="c1",
                doc_id="doc_fee",
                tenant_id=TENANT,
                kb_id=KB,
                title="제12조 연회비 면제 조건",
                text="직전 1년 이용실적 200만원 이상이면 연회비가 면제됩니다.",
            ),
        ]
    )
    return search


@pytest.fixture
async def client(engine: HybridSearchEngine):
    app = create_app(engine)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://srch") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def search(client: httpx.AsyncClient, query: str, **extra: object) -> dict:
    response = await client.post(
        "/internal/v1/search",
        json={"tenant_id": TENANT, "kb_id": KB, "query": query} | extra,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_search_returns_ranked_hits(client: httpx.AsyncClient) -> None:
    body = await search(client, "결제일 연기 되나요?")

    assert body["hits"]
    assert body["hits"][0]["chunk"]["doc_id"] == "doc_policy"


async def test_response_reports_stage_timings(client: httpx.AsyncClient) -> None:
    """단계별 지연이 없으면 예산 초과가 검색인지 리랭킹인지 알 수 없다."""
    body = await search(client, "연회비 면제")

    assert set(body) >= {"latency_ms", "dense_ms", "sparse_ms", "rerank_ms"}


async def test_top_k_limits_results(client: httpx.AsyncClient) -> None:
    body = await search(client, "카드", top_k=1)

    assert len(body["hits"]) <= 1


async def test_other_tenant_gets_nothing(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/search",
        json={"tenant_id": "intruder", "kb_id": KB, "query": "결제일 연기"},
    )

    assert response.json()["hits"] == []


async def test_empty_knowledge_base_returns_no_hits(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/search",
        json={"tenant_id": TENANT, "kb_id": "empty-kb", "query": "무엇이든"},
    )

    assert response.status_code == 200
    assert response.json()["hits"] == []


async def test_sparse_index_can_be_rebuilt_via_api(client: httpx.AsyncClient) -> None:
    """재시작 후 희소 색인 복원 경로. 없으면 검색이 조용히 반쪽이 된다."""
    response = await client.post(f"/internal/v1/kb/{KB}/rebuild", params={"tenant_id": TENANT})

    assert response.status_code == 200
    assert response.json()["indexed_chunks"] == 2
