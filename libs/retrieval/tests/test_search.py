"""검색 인프라 테스트 — 청킹·BM25·하이브리드 융합.

하이브리드 검색의 값어치는 "밀집만으로는 못 찾는 것을 찾는다"에 있으므로,
그 경우를 직접 만들어 확인한다.
"""

from __future__ import annotations

import pytest

from vai_contracts.retrieval import Chunk, SearchRequest
from vai_retrieval.bm25 import BM25Index
from vai_retrieval.chunking import ChunkingConfig, split_text
from vai_retrieval.embedding import HashingEmbedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.rerank import LexicalOverlapReranker
from vai_retrieval.store import MemoryVectorStore
from vai_retrieval.tokenize import tokenize

TENANT = "t1"
KB = "card-policy"

DOCS = [
    (
        "신용카드 결제일 변경 및 연기 규정",
        "단기 결제 연기는 연체 발생 전 고객센터 및 앱을 통해 최대 5일까지 신청 가능합니다. "
        "연체가 이미 발생한 경우에는 신청할 수 없으며 별도 상담이 필요합니다.",
    ),
    (
        "카드 분실 신고 절차",
        "카드를 분실한 경우 즉시 고객센터로 신고해야 합니다. 신고 접수 시점부터 부정사용 "
        "금액에 대한 보상이 적용됩니다.",
    ),
    (
        "제12조 연회비 면제 조건",
        "직전 1년간 이용실적이 200만원 이상인 회원은 다음 연도 연회비가 면제됩니다.",
    ),
]


def chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"c{i}",
            doc_id=f"doc{i}",
            tenant_id=TENANT,
            kb_id=KB,
            title=title,
            text=text,
        )
        for i, (title, text) in enumerate(DOCS)
    ]


@pytest.fixture
async def engine() -> HybridSearchEngine:
    embedder = HashingEmbedder()
    await embedder.initialize("", {})
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    reranker = LexicalOverlapReranker()
    await reranker.initialize("", {})
    search = HybridSearchEngine(embedder, store, reranker)
    await search.index_chunks(chunks())
    return search


async def search(engine: HybridSearchEngine, query: str, **kwargs: object):
    request = SearchRequest(tenant_id=TENANT, kb_id=KB, query=query, **kwargs)  # type: ignore[arg-type]
    return await engine.search(request)


# ── 청킹 ─────────────────────────────────────────────────────────────────────


def test_paragraphs_are_respected() -> None:
    text = "첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다."

    pieces = split_text(text, ChunkingConfig(max_chars=10, min_chars=1, overlap_chars=0))

    assert len(pieces) == 3


def test_small_paragraphs_are_packed_together() -> None:
    """짧은 줄마다 청크를 만들면 근거가 파편화되어 팝업이 쓸모없어진다."""
    text = "첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다."

    pieces = split_text(text, ChunkingConfig(max_chars=200, min_chars=1, overlap_chars=0))

    assert len(pieces) == 1


def test_article_boundary_does_not_carry_the_previous_article() -> None:
    """조항 경계에서 앞 조항을 끌고 오면 그것이 오답의 근거가 된다."""
    text = "제 11 조 목적\n\n" + "가" * 300 + "\n\n제 12 조 범위\n\n" + "나" * 300

    pieces = split_text(text, ChunkingConfig(max_chars=320, overlap_chars=100, min_chars=1))

    article_12 = next(p for p in pieces if p.lstrip().startswith("제 12 조"))
    assert "가" not in article_12


def test_oversized_paragraph_is_split_not_dropped() -> None:
    pieces = split_text("가" * 5000, ChunkingConfig(max_chars=700))

    assert pieces
    assert sum(len(p) for p in pieces) >= 5000
    assert all(len(p) <= 700 for p in pieces)


def test_empty_text_yields_no_chunks() -> None:
    assert split_text("   \n\n  ") == []


# ── 토큰화 · BM25 ─────────────────────────────────────────────────────────────


def test_particles_do_not_block_matching() -> None:
    """'결제일이'와 '결제일을'이 같은 문서를 가리켜야 한다."""
    assert set(tokenize("결제일이")) & set(tokenize("결제일을"))


def test_bm25_finds_exact_terms() -> None:
    index = BM25Index()
    index.add("a", "연회비 면제 조건")
    index.add("b", "카드 분실 신고 절차")

    ranked = index.search("연회비 면제")

    assert ranked and ranked[0][0] == "a"


def test_bm25_removal_drops_the_document() -> None:
    """문서 파기 후에도 검색에 나오면 파기 의무를 못 지킨 것이다."""
    index = BM25Index()
    index.add("a", "연회비 면제 조건")
    index.remove("a")

    assert index.search("연회비") == []
    assert index.size == 0


# ── 하이브리드 검색 ───────────────────────────────────────────────────────────


async def test_finds_the_relevant_policy(engine: HybridSearchEngine) -> None:
    response = await search(engine, "이번 달 카드 대금 결제일 연기가 가능한가요?")

    assert response.hits
    assert "결제일" in response.hits[0].chunk.title


async def test_exact_article_number_is_findable(engine: HybridSearchEngine) -> None:
    """밀집 검색만으로는 '제12조' 같은 고유 토큰을 놓친다 — 희소 축이 잡는다."""
    response = await search(engine, "제12조 연회비")

    assert response.hits
    assert "제12조" in response.hits[0].chunk.title


async def test_top_k_is_honoured(engine: HybridSearchEngine) -> None:
    response = await search(engine, "카드", top_k=2)

    assert len(response.hits) <= 2


async def test_hits_carry_stage_timings(engine: HybridSearchEngine) -> None:
    """단계별 지연이 없으면 예산 초과가 어디서 났는지 알 수 없다."""
    response = await search(engine, "결제일 연기")

    assert response.latency_ms >= 0
    assert response.hits[0].is_reranked


async def test_other_tenants_data_is_unreachable(engine: HybridSearchEngine) -> None:
    """컬렉션 분리가 깨지면 교차 테넌트 유출이다."""
    request = SearchRequest(tenant_id="other-tenant", kb_id=KB, query="결제일 연기")

    response = await engine.search(request)

    assert response.hits == []


async def test_removed_document_disappears_from_both_indexes(
    engine: HybridSearchEngine,
) -> None:
    removed = await engine.remove_document(TENANT, KB, "doc0")

    response = await search(engine, "결제일 연기 최대 5일")

    assert removed == 1
    assert all(hit.chunk.doc_id != "doc0" for hit in response.hits)


async def test_sparse_index_can_be_rebuilt_after_restart(
    engine: HybridSearchEngine,
) -> None:
    """BM25 색인은 메모리 상주라 재시작하면 사라진다. 복원되지 않으면
    검색이 밀집 축만으로 도는 '절반만 동작하는' 상태가 된다."""
    count = await engine.rebuild_sparse(TENANT, KB)

    response = await search(engine, "제12조 연회비")

    assert count == len(DOCS)
    assert response.hits


# ── 희소 전용 히트 ───────────────────────────────────────────────────────────


async def test_sparse_only_hit_is_not_dropped() -> None:
    """키워드로만 걸린 문서가 결과에 실려야 한다.

    밀집 후보 수를 좁게 잡으면 정답이 밀집 상위권에서 밀려난다. 예전 구현은
    그런 청크를 본문이 없다는 이유로 조용히 버렸고, 그러면 하이브리드 검색이
    희소 축을 갖고 있을 이유가 사라진다.
    """
    embedder = HashingEmbedder()
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    engine = HybridSearchEngine(embedder, store, LexicalOverlapReranker())
    await engine.index_chunks(chunks())

    # candidate_k=1이면 밀집은 한 건만 돌려준다 — 나머지는 희소 축에만 남는다.
    response = await search(engine, "연회비 면제 이용실적", top_k=3, candidate_k=1)

    assert any(hit.chunk.doc_id == "doc2" for hit in response.hits)


async def test_ghost_chunk_in_sparse_index_is_logged_not_silent(
    engine: HybridSearchEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """벡터 저장소에 없는 청크가 희소 색인에만 남은 경우 — 진짜 색인 불일치다."""
    engine._index(TENANT, KB).add("ghost", "연회비 면제 유령 청크")

    with caplog.at_level("WARNING"):
        await search(engine, "유령 청크")

    assert any("불일치" in record.message for record in caplog.records)


# ── 질의 확장 ────────────────────────────────────────────────────────────────


async def test_colloquial_query_reaches_the_formal_article(
    engine: HybridSearchEngine,
) -> None:
    """ "잃어버렸어요"와 "분실"은 글자가 하나도 겹치지 않는다.

    사전이 없으면 어휘 검색으로는 영원히 못 만난다.
    """
    response = await search(engine, "카드 잃어버렸어요 어떻게 하죠")

    assert response.expanded_terms
    assert response.hits[0].chunk.doc_id == "doc1"


async def test_expansion_can_be_disabled() -> None:
    from vai_retrieval.lexicon import QueryExpander

    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    engine = HybridSearchEngine(
        HashingEmbedder(), store, LexicalOverlapReranker(), expander=QueryExpander(enabled=False)
    )
    await engine.index_chunks(chunks())

    response = await search(engine, "카드 잃어버렸어요")

    assert response.expanded_terms == []


# ── 희소 색인 자가 복원 ──────────────────────────────────────────────────────


async def _store_only(store: MemoryVectorStore) -> HybridSearchEngine:
    """색인은 다른 프로세스(RAG-KB)에서 끝났고, 이 프로세스의 BM25는 비어 있다."""
    vectors = await HashingEmbedder().embed([chunk.text for chunk in chunks()])
    await store.upsert(chunks(), vectors)
    return HybridSearchEngine(HashingEmbedder(), store, LexicalOverlapReranker())


async def test_empty_sparse_index_is_rebuilt_on_first_query() -> None:
    """재시작 후 첫 질의에서 스스로 복원해야 한다.

    복원 전 상태는 **오류를 내지 않는다** — 밀집 축만으로 결과가 나오고 희소
    점수만 전부 0이다. 장애로 보이지 않는 장애가 가장 오래 간다.
    """
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    engine = await _store_only(store)
    assert engine._index(TENANT, KB).size == 0

    response = await search(engine, "연회비 면제 이용실적")

    assert engine._index(TENANT, KB).size == len(DOCS)
    assert any(hit.sparse_score > 0 for hit in response.hits), "희소 축이 살아나야 한다"


async def test_rebuild_is_attempted_once_for_an_empty_collection() -> None:
    """정말로 문서가 없는 컬렉션에 매 질의마다 복원을 시도하면 검색이 느려진다."""
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    engine = HybridSearchEngine(HashingEmbedder(), store, LexicalOverlapReranker())

    calls = 0
    original = engine.rebuild_sparse

    async def counted(tenant_id: str, kb_id: str) -> int:
        nonlocal calls
        calls += 1
        return await original(tenant_id, kb_id)

    engine.rebuild_sparse = counted  # type: ignore[method-assign]
    await search(engine, "무엇이든")
    await search(engine, "또 무엇이든")

    assert calls == 1


async def test_freshly_indexed_collection_is_not_rescanned(
    engine: HybridSearchEngine,
) -> None:
    """방금 색인한 컬렉션까지 저장소를 훑으면 색인 직후 첫 질의가 느려진다."""
    calls = 0
    original = engine.rebuild_sparse

    async def counted(tenant_id: str, kb_id: str) -> int:
        nonlocal calls
        calls += 1
        return await original(tenant_id, kb_id)

    engine.rebuild_sparse = counted  # type: ignore[method-assign]
    await search(engine, "연회비")

    assert calls == 0
