"""평가 실행기.

블록을 HTTP로 띄우지 않고 **in-process로 로직을 직접 돌린다.** 이유는 두 가지다:

1. **결정성.** 프로세스·Redis·네트워크가 끼면 같은 입력에 같은 숫자가 나오지
   않는다. 회귀 게이트가 흔들리면 사람은 그 게이트를 끈다.
2. **측정 대상의 분리.** 여기서 재는 것은 알고리즘 품질이지 배포 품질이 아니다.
   배포 경로(컨슈머 그룹, 라이선스 게이트, 웹소켓)는 스택 스모크 테스트가 본다.

그래서 여기 지연 수치는 **알고리즘 지연**이다. 서비스 응답 시간이 아니다 —
그 둘을 같은 칸에 적으면 아무도 어느 쪽이 느려졌는지 모르게 된다.
"""

from __future__ import annotations

import time
from pathlib import Path

from evalctl.dataset import Article, GroundingCase, PiiCase, RetrievalCase, TtsCase
from evalctl.metrics import CaseOutcome, CheckOutcome, rank_of
from vai_contracts.retrieval import Chunk, SearchRequest
from vai_flt_micro.filter import MicroComplianceFilter
from vai_retrieval.chunking import split_text
from vai_retrieval.embedding import HashingEmbedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.lexicon import QueryExpander
from vai_retrieval.rerank import LexicalOverlapReranker
from vai_retrieval.store import MemoryVectorStore
from vai_ta_assist.answer import Evidence, verify
from vai_tts_core.normalize import normalize

EVAL_TENANT = "eval"
EVAL_KB = "golden"

DEFAULT_TOP_K = 3
"""상담원 화면에 뜨는 카드 수(사양서 §3 모듈 3). 평가도 같은 값으로 한다.

여기를 10으로 올리면 점수는 오르지만 그 시스템은 출하되지 않는다.
"""

CANDIDATE_K = 10


async def build_engine(articles: list[Article], *, expand: bool = True) -> HybridSearchEngine:
    """코퍼스를 색인한 검색 엔진을 만든다.

    임베더는 :class:`HashingEmbedder`다 — 모델 없이 도는 어휘 임베딩이라
    절대 점수는 로컬 임베딩 모델보다 낮다. 그래도 **변화**는 정확히 잡힌다.
    베이스라인이 재는 것은 절대 점수가 아니라 변화다.
    """
    embedder = HashingEmbedder()
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    engine = HybridSearchEngine(
        embedder,
        store,
        LexicalOverlapReranker(),
        expander=QueryExpander(enabled=expand),
    )

    chunks: list[Chunk] = []
    for article in articles:
        for ordinal, piece in enumerate(split_text(article.text)):
            chunks.append(
                Chunk(
                    chunk_id=f"{article.article_id}#{ordinal}",
                    doc_id=article.article_id,
                    tenant_id=EVAL_TENANT,
                    kb_id=EVAL_KB,
                    title=article.title,
                    text=piece,
                    ordinal=ordinal,
                )
            )
    await engine.index_chunks(chunks)
    return engine


async def run_retrieval(
    cases: list[RetrievalCase],
    articles: list[Article],
    top_k: int = DEFAULT_TOP_K,
    *,
    expand: bool = True,
) -> list[CaseOutcome]:
    engine = await build_engine(articles, expand=expand)

    outcomes: list[CaseOutcome] = []
    for case in cases:
        started = time.perf_counter()
        response = await engine.search(
            SearchRequest(
                tenant_id=EVAL_TENANT,
                kb_id=EVAL_KB,
                query=case.query,
                top_k=top_k,
                candidate_k=CANDIDATE_K,
            )
        )
        elapsed = (time.perf_counter() - started) * 1000

        # 상위 top_k **청크**를 조항으로 접는다. 한 조항이 두 청크를 차지하면
        # 상담원이 보는 서로 다른 조항은 그만큼 줄어든다 — 그 손해를 그대로 센다.
        returned: list[str] = []
        for hit in response.hits:
            if hit.chunk.doc_id not in returned:
                returned.append(hit.chunk.doc_id)

        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                query=case.query,
                rank=rank_of(returned, case.expected),
                returned=returned,
                expected=list(case.expected),
                latency_ms=elapsed,
                note=case.note,
            )
        )
    return outcomes


def run_pii(cases: list[PiiCase]) -> list[CheckOutcome]:
    """마스킹 채점. **놓친 것과 과하게 지운 것을 같은 무게로 센다.**

    미탐은 유출이고 과탐은 상담 불능이다. 한쪽만 재면 다른 쪽이 반드시 나빠진다.
    """
    filt = MicroComplianceFilter()

    outcomes: list[CheckOutcome] = []
    for case in cases:
        started = time.perf_counter()
        result = filt.process_text(case.text)
        elapsed = (time.perf_counter() - started) * 1000

        problems: list[str] = []
        found = sorted(set(result.pii_types))
        wanted = sorted(set(case.expect_types))
        if found != wanted:
            missed = sorted(set(wanted) - set(found))
            extra = sorted(set(found) - set(wanted))
            if missed:
                problems.append(f"미탐 {missed}")
            if extra:
                problems.append(f"과탐 {extra}")
        for secret in case.must_not_contain:
            if secret in result.clean_text:
                problems.append(f"원문 잔존 {secret!r}")

        outcomes.append(
            CheckOutcome(
                case_id=case.case_id,
                passed=not problems,
                detail=", ".join(problems),
                known_limitation=case.known_limitation,
                latency_ms=elapsed,
            )
        )
    return outcomes


def run_tts(cases: list[TtsCase]) -> list[CheckOutcome]:
    """읽기 규칙 채점. 문자열 완전 일치다.

    부분 점수를 주지 않는 이유: 숫자 하나를 잘못 읽으면 그 통화는 실패한 통화다.
    "거의 맞음"이라는 상태가 없다.
    """
    outcomes: list[CheckOutcome] = []
    for case in cases:
        started = time.perf_counter()
        actual = normalize(case.text)
        elapsed = (time.perf_counter() - started) * 1000
        passed = actual == case.expect
        outcomes.append(
            CheckOutcome(
                case_id=case.case_id,
                passed=passed,
                detail="" if passed else f"기대 {case.expect!r} ≠ 실제 {actual!r}",
                latency_ms=elapsed,
            )
        )
    return outcomes


def run_grounding(cases: list[GroundingCase]) -> list[CheckOutcome]:
    """추천 답변 검증기 채점.

    LLM을 부르지 않는다 — 재는 것은 생성 품질이 아니라 **검증기의 판단**이다.
    환각을 통과시켰는가(고객에게 틀린 숫자가 전달된다), 멀쩡한 답변을 버렸는가
    (상담원이 쓸 수 있는 답을 못 쓰게 된다). 둘 다 실패로 센다.
    """
    outcomes: list[CheckOutcome] = []
    for case in cases:
        evidence = [
            Evidence(index=i + 1, doc_id=f"d{i + 1}", title="", text=text)
            for i, text in enumerate(case.evidence)
        ]
        started = time.perf_counter()
        verdict = verify(case.answer, evidence) if evidence else None
        elapsed = (time.perf_counter() - started) * 1000

        accepted = bool(verdict and verdict.accepted)
        passed = accepted == case.accept
        if passed:
            detail = ""
        elif case.accept:
            reason = verdict.reason if verdict else "근거 없음"
            detail = f"멀쩡한 답변을 버렸다 — {reason}"
        else:
            detail = "환각을 통과시켰다"

        outcomes.append(
            CheckOutcome(
                case_id=case.case_id,
                passed=passed,
                detail=detail,
                known_limitation=case.known_limitation,
                latency_ms=elapsed,
            )
        )
    return outcomes


def corpus_path(eval_dir: Path, corpus_name: str) -> Path:
    return eval_dir / "corpus" / corpus_name
