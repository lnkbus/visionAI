"""RAG-KB 문서 수집 계약 테스트."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

from vai_contracts.retrieval import Document, DocumentStatus, SearchRequest
from vai_rag_kb.app import create_app
from vai_rag_kb.indexer import InMemoryDocumentStore
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
    app = create_app(engine, InMemoryDocumentStore())
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


# --- 파일 업로드 ---------------------------------------------------------


def _hwpx_bytes(paragraphs: list[str]) -> bytes:
    import zipfile
    from io import BytesIO

    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    body = "".join(f"<hp:p><hp:run><hp:t>{p}</hp:t></hp:run></hp:p>" for p in paragraphs)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Contents/section0.xml", f"<hp:sec {ns}>{body}</hp:sec>")
    return buffer.getvalue()


async def test_HWPX_약관을_올리면_검색된다(client: httpx.AsyncClient) -> None:
    """공공 조달의 핵심 경로: 문서 파일 → 색인 → 검색.

    평문만 받으면 고객사는 "문서를 넣어 보세요"의 첫 단계에서 막힌다.
    """
    payload = _hwpx_bytes(["제 1 조 결제일 연기", "단기 결제 연기는 최대 5일까지 신청 가능합니다."])
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents/upload",
        files={"file": ("약관.hwpx", payload, "application/octet-stream")},
        data={"tenant_id": TENANT, "title": "카드 약관"},
    )
    assert response.status_code == 202
    doc_id = response.json()["doc_id"]

    for _ in range(60):
        status = (await client.get(f"/internal/v1/documents/{doc_id}")).json()
        if status["status"] in (DocumentStatus.READY, DocumentStatus.FAILED):
            break
        await asyncio.sleep(0.05)
    assert status["status"] == DocumentStatus.READY
    assert status["chunk_count"] > 0


async def test_읽을_수_없는_문서는_400으로_즉시_알린다(client: httpx.AsyncClient) -> None:
    """빈 텍스트로 색인을 '성공' 처리하면 고객사는 문서를 넣었는데
    검색이 안 되는 이유를 영영 모른다."""
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents/upload",
        files={"file": ("깨진문서.hwp", b"\xd0\xcf\x11\xe0not really", "application/octet-stream")},
        data={"tenant_id": TENANT},
    )
    assert response.status_code == 400
    assert "CFB" in response.json()["detail"] or "HWP" in response.json()["detail"]


async def test_빈_파일은_거부한다(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents/upload",
        files={"file": ("empty.txt", b"", "text/plain")},
        data={"tenant_id": TENANT},
    )
    assert response.status_code == 400


async def test_추출_경고가_문서에_남는다(client: httpx.AsyncClient) -> None:
    """온전하지 않게 읽힌 부분을 조용히 넘기면 '왜 이 조항만 검색이 안 되지'를
    아무도 설명하지 못한다."""
    response = await client.post(
        f"/internal/v1/kb/{KB}/documents/upload",
        files={"file": ("약관.txt", "제1조 목적".encode("cp949"), "text/plain")},
        data={"tenant_id": TENANT},
    )
    assert response.status_code == 202
    assert any("cp949" in w for w in response.json()["warnings"])


async def test_지원_형식을_알려_준다(client: httpx.AsyncClient) -> None:
    """고객사가 반입 전에 확인한다."""
    body = (await client.get("/internal/v1/parsers")).json()
    assert "hwp" in body["parsers"] and "pdf" in body["parsers"]


# ── 재기동을 넘는 파기 가능성 ────────────────────────────────────────────────


async def test_redis_document_store_survives_restart() -> None:
    """메타데이터를 잃으면 파기가 불가능해진다.

    벡터 저장소에는 청크가 그대로 남아 검색은 계속 그 문서로 답하는데,
    목록에는 없어서 지울 대상을 특정할 수 없다 — "그런 문서 없습니다"라면서
    그 내용으로 답변하는 상태가 된다.
    """
    import redis.asyncio as aioredis

    from vai_rag_kb.indexer import RedisDocumentStore

    client = aioredis.Redis(db=14)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis가 없다")

    try:
        await client.flushdb()
        document = Document(doc_id="doc_x", tenant_id="t1", kb_id="kb", title="개인정보 문서")
        await RedisDocumentStore(client).put(document)

        # 블록 재기동 — 새 인스턴스가 이전 메타데이터를 이어 본다.
        restarted = RedisDocumentStore(client)

        assert (await restarted.get("doc_x")) is not None
        assert [d.doc_id for d in await restarted.list_for("t1", "kb")] == ["doc_x"]
        assert (await restarted.delete("doc_x")) is not None
        assert await restarted.list_for("t1", "kb") == []
    finally:
        await client.flushdb()
        await client.aclose()


async def test_purge_works_without_metadata(engine: HybridSearchEngine) -> None:
    """파기 요구는 거절할 수 없다 — 메타데이터가 없어도 지울 수 있어야 한다."""
    app = create_app(engine, InMemoryDocumentStore())
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rag-kb") as client,
        app.router.lifespan_context(app),
    ):
        created = await client.post(
            "/internal/v1/kb/kb1/documents/sync",
            json={"tenant_id": "t1", "title": "약관", "text": "재발급 수수료는 3,000원이다. " * 5},
        )
        doc_id = created.json()["doc_id"]

        # 메타데이터만 사라진 상태를 만든다(재기동으로 인메모리 저장소를 잃은 경우).
        await app.state.documents.delete(doc_id)

        blind = await client.request(
            "DELETE", f"/internal/v1/documents/{doc_id}", params={"tenant_id": "t1", "kb_id": "kb1"}
        )

        assert blind.status_code == 200
        assert blind.json()["removed_chunks"] > 0, "청크가 실제로 지워져야 한다"


async def test_delete_without_hints_still_404s(engine: HybridSearchEngine) -> None:
    """아무 단서 없이 지우라고 하면 무엇을 지울지 알 수 없다."""
    app = create_app(engine, InMemoryDocumentStore())
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rag-kb") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.delete("/internal/v1/documents/doc_없음")

    assert response.status_code == 404
