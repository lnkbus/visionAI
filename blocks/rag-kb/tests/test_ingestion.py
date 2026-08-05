"""RAG-KB 문서 수집 계약 테스트."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from vai_contracts.retrieval import DocumentStatus, SearchRequest
from vai_rag_kb.app import create_app
from vai_retrieval.embedding import HashingEmbedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.rerank import LexicalOverlapReranker
from vai_retrieval.store import MemoryVectorStore

TENANT = "t1"
KB = "card"
POLICY = (
    "제 1 조 결제일 연기\n\n"
    "단기 결제 연기는 연체 발생 전 고객센터 및 앱을 통해 최대 5일까지 신청 가능합니다.\n\n"
    "제 2 조 연회비\n\n"
    "직전 1년간 이용실적이 200만원 이상이면 다음 연도 연회비가 면제됩니다."
)


@pytest.fixture
async def engine() -> HybridSearchEngine:
    embedder = HashingEmbedder()
    await embedder.initialize("", {})
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    reranker = LexicalOverlapReranker()
    await reranker.initialize("", {})
    return HybridSearchEngine(embedder, store, reranker)


@pytest.fixture
async def client(engine: HybridSearchEngine):
    app = create_app(engine)
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://kb") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def ingest(client: httpx.AsyncClient, text: str = POLICY, title: str = "카드 약관") -> dict:
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents/sync",
        json={"tenant_id": TENANT, "title": title, "text": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_ingested_document_becomes_searchable(
    client: httpx.AsyncClient, engine: HybridSearchEngine
) -> None:
    document = await ingest(client)

    response = await engine.search(
        SearchRequest(tenant_id=TENANT, kb_id=KB, query="결제일 연기 최대 며칠")
    )

    assert document["status"] == DocumentStatus.READY.value
    assert document["chunk_count"] >= 2
    assert response.hits
    assert "5일" in response.hits[0].chunk.text


async def test_empty_document_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents",
        json={"tenant_id": TENANT, "title": "빈 문서", "text": "   "},
    )

    assert response.status_code == 400


async def test_status_is_queryable_while_indexing(client: httpx.AsyncClient) -> None:
    """관리자가 '왜 검색이 안 되지'라고 물을 때 답할 수 있어야 한다."""
    document = await ingest(client)

    response = await client.get(f"/internal/v1/documents/{document['doc_id']}")

    assert response.status_code == 200
    assert response.json()["status"] == DocumentStatus.READY.value


async def test_unknown_document_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/internal/v1/documents/doc_nope")).status_code == 404


async def test_deleting_a_document_removes_its_chunks(
    client: httpx.AsyncClient, engine: HybridSearchEngine
) -> None:
    """원본을 지웠는데 청크가 남으면 파기 의무를 못 지킨다(docs/05 §2.2)."""
    document = await ingest(client)

    deleted = await client.delete(f"/internal/v1/documents/{document['doc_id']}")
    response = await engine.search(SearchRequest(tenant_id=TENANT, kb_id=KB, query="결제일 연기"))

    assert deleted.status_code == 200
    assert deleted.json()["removed_chunks"] == document["chunk_count"]
    assert response.hits == []
    assert (await client.get(f"/internal/v1/documents/{document['doc_id']}")).status_code == 404


async def test_documents_are_listed_per_tenant(client: httpx.AsyncClient) -> None:
    await ingest(client, title="카드 약관")

    mine = await client.get(f"/internal/v1/kb/{KB}/documents", params={"tenant_id": TENANT})
    theirs = await client.get(f"/internal/v1/kb/{KB}/documents", params={"tenant_id": "other"})

    assert len(mine.json()) == 1
    assert theirs.json() == []


async def test_article_boundaries_survive_chunking(
    client: httpx.AsyncClient, engine: HybridSearchEngine
) -> None:
    """조항이 섞인 청크를 인용하면 어느 조항이 근거인지 흐려진다."""
    await ingest(client)

    response = await engine.search(
        SearchRequest(tenant_id=TENANT, kb_id=KB, query="연회비 면제 이용실적")
    )

    assert response.hits
    assert "연회비" in response.hits[0].chunk.text
    assert "결제일 연기" not in response.hits[0].chunk.text
