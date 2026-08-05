"""골든셋 적재.

JSONL을 쓰는 이유는 한 줄 = 한 사례라서 **diff가 사례 단위로 읽히기** 때문이다.
평가셋은 시간이 지나면서 계속 늘어나고, 어떤 사례가 언제 왜 추가·수정됐는지가
곧 품질 이력이 된다. YAML 배열이면 들여쓰기 한 칸 때문에 무관한 줄이 diff에 섞인다.

로딩에서 지키는 원칙 하나: **조용히 건너뛰지 않는다.** ``expect_types``를
``expect_type``으로 오타 내면 그 사례는 "아무것도 기대하지 않음"이 되어 항상
통과한다. 평가셋에서 그런 사례는 없는 것만 못하다 — 알 수 없는 키는 즉시 실패다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_EVAL_DIR = "eval"
CORPUS_SUBDIR = "corpus"


class DatasetError(RuntimeError):
    """골든셋 파일이 규격을 벗어났다."""


@dataclass(frozen=True)
class RetrievalCase:
    """검색 사례. ``expected``는 문서 조각이 아니라 **조항 ID**다.

    청크 ID로 정답을 적으면 청킹 파라미터를 바꾸는 순간 골든셋 전체가 무효가 된다.
    조항은 원문이 개정되지 않는 한 그대로다.
    """

    case_id: str
    query: str
    expected: list[str]
    note: str = ""


@dataclass(frozen=True)
class PiiCase:
    case_id: str
    text: str
    expect_types: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    known_limitation: bool = False
    """현재 못 잡는다는 사실을 고정한 사례.

    통과했다고 '정확하다'가 아니다 — 리포트에서 따로 세어 성공 건수에 섞이지
    않게 한다. 나중에 실제로 잡히게 되면 이 사례가 깨지고, 그때 골든셋을 고친다.
    """

    note: str = ""


@dataclass(frozen=True)
class TtsCase:
    case_id: str
    text: str
    expect: str
    note: str = ""


@dataclass(frozen=True)
class Article:
    """코퍼스의 인용 단위. 검색 정답이 가리키는 대상이다."""

    article_id: str
    title: str
    text: str


_RETRIEVAL_KEYS = {"case_id", "query", "expected", "note"}
_PII_KEYS = {"case_id", "text", "expect_types", "must_not_contain", "known_limitation", "note"}
_TTS_KEYS = {"case_id", "text", "expect", "note"}


def _read_rows(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """헤더(메타) 한 줄과 사례 줄들을 분리한다."""
    if not path.is_file():
        raise DatasetError(f"골든셋 파일이 없다: {path}")

    header: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{lineno} JSON 파싱 실패: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DatasetError(f"{path.name}:{lineno} 객체가 아니다")
        if all(key.startswith("_") for key in parsed):
            header.update(parsed)
            continue
        if "case_id" not in parsed:
            raise DatasetError(f"{path.name}:{lineno} case_id가 없다")
        rows.append(parsed)
    return header, rows


def _check_keys(path: Path, rows: list[dict[str, Any]], allowed: set[str]) -> None:
    seen: set[str] = set()
    for row in rows:
        unknown = set(row) - allowed
        if unknown:
            raise DatasetError(
                f"{path.name} [{row['case_id']}] 알 수 없는 키 {sorted(unknown)} — "
                "오타 난 키는 조용히 무시되면 사례가 항상 통과한다"
            )
        case_id = str(row["case_id"])
        if case_id in seen:
            raise DatasetError(f"{path.name} case_id 중복: {case_id}")
        seen.add(case_id)


def load_retrieval(path: Path) -> tuple[list[RetrievalCase], str]:
    """검색 골든셋과 헤더가 지정한 코퍼스 파일명을 돌려준다."""
    header, rows = _read_rows(path)
    _check_keys(path, rows, _RETRIEVAL_KEYS)
    corpus = str(header.get("_corpus", ""))
    if not corpus:
        raise DatasetError(f"{path.name}: 헤더에 _corpus가 없다 — 어떤 문서로 평가할지 불명확하다")

    cases: list[RetrievalCase] = []
    for row in rows:
        expected = [str(item) for item in row.get("expected", [])]
        if not expected:
            raise DatasetError(f"{path.name} [{row['case_id']}] expected가 비었다")
        cases.append(
            RetrievalCase(
                case_id=str(row["case_id"]),
                query=str(row["query"]),
                expected=expected,
                note=str(row.get("note", "")),
            )
        )
    return cases, corpus


def load_pii(path: Path) -> list[PiiCase]:
    _, rows = _read_rows(path)
    _check_keys(path, rows, _PII_KEYS)
    return [
        PiiCase(
            case_id=str(row["case_id"]),
            text=str(row["text"]),
            expect_types=[str(item) for item in row.get("expect_types", [])],
            must_not_contain=[str(item) for item in row.get("must_not_contain", [])],
            known_limitation=bool(row.get("known_limitation", False)),
            note=str(row.get("note", "")),
        )
        for row in rows
    ]


def load_tts(path: Path) -> list[TtsCase]:
    _, rows = _read_rows(path)
    _check_keys(path, rows, _TTS_KEYS)
    return [
        TtsCase(
            case_id=str(row["case_id"]),
            text=str(row["text"]),
            expect=str(row["expect"]),
            note=str(row.get("note", "")),
        )
        for row in rows
    ]


def load_corpus(path: Path) -> list[Article]:
    """마크다운 코퍼스를 조항 단위로 자른다.

    ``## 제3조 (카드의 발급과 재발급)`` 형태의 제목을 경계로 본다. 실제 약관
    PDF를 넣을 때도 같은 형태로 전처리하면 골든셋을 그대로 재사용할 수 있다.
    """
    if not path.is_file():
        raise DatasetError(f"코퍼스 파일이 없다: {path}")

    articles: list[Article] = []
    current_id = ""
    current_title = ""
    buffer: list[str] = []

    def flush() -> None:
        if current_id and buffer:
            articles.append(
                Article(
                    article_id=current_id,
                    title=current_title,
                    text="\n".join(buffer).strip(),
                )
            )

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            current_id = heading.split(" ", 1)[0].strip()
            current_title = heading
            buffer = []
            continue
        if current_id:
            buffer.append(line)
    flush()

    if not articles:
        raise DatasetError(f"{path.name}: '## 제N조' 제목을 하나도 찾지 못했다")
    return articles


def check_coverage(cases: list[RetrievalCase], articles: list[Article]) -> list[str]:
    """골든셋이 가리키는 조항이 코퍼스에 실제로 있는지 본다.

    오타 난 정답("제3조 "→"제3죠")은 영원히 못 맞히는 사례가 되어, 실력과
    무관하게 점수를 깎는다. 그런 사례가 섞인 평가셋은 신뢰를 잃는다.
    """
    known = {article.article_id for article in articles}
    problems = [
        f"[{case.case_id}] 코퍼스에 없는 정답: {item}"
        for case in cases
        for item in case.expected
        if item not in known
    ]
    covered = {item for case in cases for item in case.expected}
    problems.extend(
        f"코퍼스 조항 {article_id}를 검증하는 사례가 없다" for article_id in sorted(known - covered)
    )
    return problems
