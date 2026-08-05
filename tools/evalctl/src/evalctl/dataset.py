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
class GroundingCase:
    """추천 답변 검증 사례.

    LLM을 부르지 않는다. 재는 것은 **생성 품질이 아니라 검증기의 판단**이다 —
    환각을 통과시키는가, 멀쩡한 답변을 버리는가. 그 둘은 모델과 무관하게
    결정적으로 측정할 수 있고, 그래야 회귀 게이트가 흔들리지 않는다.
    """

    case_id: str
    evidence: list[str]
    answer: str
    accept: bool
    """**현재 검증기가 내는 판정**을 적는다.

    ``known_limitation``이 붙은 사례에서는 이 값이 '옳은 판정'이 아니라
    '지금의 판정'이다 — 구멍을 기록으로 고정해 두어야 나중에 막았을 때
    이 사례가 깨지고, 그때 골든셋을 갱신하게 된다."""

    known_limitation: bool = False
    note: str = ""


@dataclass(frozen=True)
class IntentCase:
    """음성봇 의도 라우팅 사례.

    ``expect``가 ``None``이면 **기권해야 한다**는 뜻이다. 잘못 라우팅하는 것보다
    되묻는 편이 낫다는 판단을 사례로 고정한다 — 되묻기는 고객이 다시 말하면
    회복되지만, 오라우팅은 엉뚱한 안내를 끝까지 듣고 나서야 드러난다.
    """

    case_id: str
    intents: list[dict[str, Any]]
    utterance: str
    expect: str | None
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
_GROUNDING_KEYS = {"case_id", "evidence", "answer", "accept", "known_limitation", "note"}
_INTENT_KEYS = {"case_id", "intents", "utterance", "expect", "note"}


@dataclass(frozen=True)
class AsrCase:
    """음성인식 사례 — 오디오 파일 하나와 정답 전사.

    오디오를 레포에 넣지 않는다. 저작권과 개인정보가 걸리고, 무엇보다 커진다.
    매니페스트는 **상대 경로**만 들고, 실제 오디오는 각자 반입한다
    (`eval/audio/README.md`).
    """

    case_id: str
    audio: str
    text: str
    note: str = ""


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


def load_grounding(path: Path) -> list[GroundingCase]:
    _, rows = _read_rows(path)
    _check_keys(path, rows, _GROUNDING_KEYS)
    for row in rows:
        if "accept" not in row:
            raise DatasetError(f"{path.name} [{row['case_id']}] accept가 없다 — 기대 판정이 없다")
    return [
        GroundingCase(
            case_id=str(row["case_id"]),
            evidence=[str(item) for item in row.get("evidence", [])],
            answer=str(row["answer"]),
            accept=bool(row["accept"]),
            known_limitation=bool(row.get("known_limitation", False)),
            note=str(row.get("note", "")),
        )
        for row in rows
    ]


def load_intent(path: Path) -> list[IntentCase]:
    _, rows = _read_rows(path)
    _check_keys(path, rows, _INTENT_KEYS)
    for row in rows:
        if "expect" not in row:
            raise DatasetError(
                f"{path.name} [{row['case_id']}] expect가 없다 — "
                "기권이 정답인 사례는 null을 명시한다"
            )
        if not row.get("intents"):
            raise DatasetError(f"{path.name} [{row['case_id']}] intents가 비었다")
    return [
        IntentCase(
            case_id=str(row["case_id"]),
            intents=list(row["intents"]),
            utterance=str(row["utterance"]),
            expect=None if row["expect"] is None else str(row["expect"]),
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


_ASR_KEYS = {"case_id", "audio", "text", "note"}


def load_asr(path: Path) -> tuple[list[AsrCase], str]:
    """오디오 매니페스트. 헤더의 ``_audio_root`` 가 오디오 디렉토리를 가리킨다."""
    header, rows = _read_rows(path)
    _check_keys(path, rows, _ASR_KEYS)
    cases = [
        AsrCase(
            case_id=str(row["case_id"]),
            audio=str(row["audio"]),
            text=str(row["text"]),
            note=str(row.get("note", "")),
        )
        for row in rows
    ]
    for case in cases:
        if not case.text.strip():
            raise DatasetError(f"{path.name} [{case.case_id}] 정답 전사가 비었다")
    return cases, str(header.get("_audio_root", "audio"))
