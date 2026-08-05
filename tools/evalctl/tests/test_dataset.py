"""골든셋 적재 검증.

핵심은 **조용한 실패가 없다**는 것이다. 오타 난 사례가 '항상 통과'로 변하는
순간 평가셋은 숫자를 올려 주는 장식이 된다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalctl.dataset import (
    DatasetError,
    check_coverage,
    load_corpus,
    load_pii,
    load_retrieval,
    load_tts,
)

REPO_EVAL = Path(__file__).resolve().parents[3] / "eval"


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "pii.jsonl",
        [{"case_id": "typo", "text": "주민번호 900101-1234567", "expect_type": ["rrn"]}],
    )
    with pytest.raises(DatasetError, match="알 수 없는 키"):
        load_pii(path)


def test_duplicate_case_id_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "tts.jsonl",
        [
            {"case_id": "dup", "text": "1개", "expect": "한 개"},
            {"case_id": "dup", "text": "2개", "expect": "두 개"},
        ],
    )
    with pytest.raises(DatasetError, match="중복"):
        load_tts(path)


def test_case_without_id_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "tts.jsonl", [{"text": "1개", "expect": "한 개"}])
    with pytest.raises(DatasetError, match="case_id"):
        load_tts(path)


def test_metadata_header_is_not_a_case(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "retrieval.jsonl",
        [
            {"_comment": "설명", "_corpus": "x.md"},
            {"case_id": "a", "query": "질문", "expected": ["제1조"]},
        ],
    )
    cases, corpus = load_retrieval(path)
    assert corpus == "x.md"
    assert [case.case_id for case in cases] == ["a"]


def test_retrieval_requires_corpus_header(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.jsonl", [{"case_id": "a", "query": "q", "expected": ["제1조"]}])
    with pytest.raises(DatasetError, match="_corpus"):
        load_retrieval(path)


def test_retrieval_rejects_empty_expected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "r.jsonl",
        [{"_corpus": "x.md"}, {"case_id": "a", "query": "q", "expected": []}],
    )
    with pytest.raises(DatasetError, match="expected"):
        load_retrieval(path)


def test_corpus_splits_on_article_headings(tmp_path: Path) -> None:
    path = tmp_path / "terms.md"
    path.write_text(
        "# 제목\n\n서문\n\n## 제1조 (목적)\n\n본문 하나\n\n## 제2조 (정의)\n\n본문 둘\n",
        encoding="utf-8",
    )
    articles = load_corpus(path)
    assert [a.article_id for a in articles] == ["제1조", "제2조"]
    assert "본문 하나" in articles[0].text
    # 제목 앞의 서문은 어느 조항에도 속하지 않는다 — 인용 단위가 아니다.
    assert "서문" not in articles[0].text


def test_corpus_without_headings_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "flat.md"
    path.write_text("조항 구분이 없는 문서\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="제목"):
        load_corpus(path)


def test_coverage_catches_answer_typo(tmp_path: Path) -> None:
    corpus = tmp_path / "c.md"
    corpus.write_text("## 제1조 (목적)\n\n본문\n", encoding="utf-8")
    dataset = _write(
        tmp_path / "r.jsonl",
        [{"_corpus": "c.md"}, {"case_id": "a", "query": "q", "expected": ["제9조"]}],
    )
    cases, _ = load_retrieval(dataset)
    problems = check_coverage(cases, load_corpus(corpus))
    assert any("코퍼스에 없는 정답" in problem for problem in problems)


def test_repo_golden_sets_load() -> None:
    """레포에 실제로 들어 있는 골든셋이 규격을 만족하는지 — CI 게이트."""
    cases, corpus_name = load_retrieval(REPO_EVAL / "retrieval-terms.jsonl")
    articles = load_corpus(REPO_EVAL / "corpus" / corpus_name)
    assert len(cases) >= 30
    assert not [p for p in check_coverage(cases, articles) if "없는 정답" in p]
    assert len(load_pii(REPO_EVAL / "pii-masking.jsonl")) >= 15
    assert len(load_tts(REPO_EVAL / "tts-reading.jsonl")) >= 15
