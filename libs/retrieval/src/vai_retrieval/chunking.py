"""문서 청킹.

약관·매뉴얼은 조항 단위로 의미가 끊긴다. 고정 길이로 자르면 조항 하나가
두 청크에 걸쳐 어느 쪽도 근거로 쓸 수 없게 되므로, 문단 경계를 우선 존중하고
그 안에서만 길이 제한을 적용한다.

인접 청크를 일부 겹치게(overlap) 두는 이유도 같다: 경계 바로 옆 문장이
답의 핵심인 경우가 잦다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 빈 줄 = 문단 경계. 한국어 문서에서 가장 신뢰할 만한 구조 신호다.
_PARAGRAPH = re.compile(r"\n\s*\n")
# 조항 머리("제 12 조", "3.", "-." 등)는 그 자체로 강한 경계다.
_ARTICLE_HEAD = re.compile(r"^\s*(제\s*\d+\s*조|\d+\.\s|-\.\s|[가-힣]\.\s)")
_SENTENCE = re.compile(r"(?<=[.!?。])\s+|(?<=다\.)\s+")


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 700
    """한 청크의 상한. 임베딩 모델 입력 길이와 팝업 표시 분량을 함께 고려한 값."""

    overlap_chars: int = 120
    min_chars: int = 40
    """이보다 짧은 조각은 앞 청크에 붙인다 — 제목만 남은 청크는 검색을 방해한다."""


def split_text(text: str, config: ChunkingConfig | None = None) -> list[str]:
    """텍스트를 검색 단위로 나눈다."""
    cfg = config or ChunkingConfig()
    units = _structural_units(text, cfg)
    if not units:
        return []

    chunks: list[str] = []
    current = ""
    for unit in units:
        starts_article = bool(_ARTICLE_HEAD.match(unit))
        if not current:
            current = unit
        elif starts_article:
            # 조항 머리는 항상 새 청크를 연다. 자리가 남는다고 앞 조항 뒤에
            # 붙이면 한 청크가 두 조항을 담아, 인용했을 때 어느 조항이 근거인지
            # 흐려진다.
            chunks.append(current)
            current = unit
        elif len(current) + len(unit) + 1 <= cfg.max_chars:
            current = f"{current}\n{unit}"
        else:
            chunks.append(current)
            current = _with_overlap(current, unit, cfg)

    if current:
        chunks.append(current)

    return _merge_runts(chunks, cfg)


def _structural_units(text: str, cfg: ChunkingConfig) -> list[str]:
    """문단 → (너무 길면) 문장 → (그래도 길면) 강제 절단 순으로 잘게 만든다."""
    units: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        stripped = paragraph.strip()
        if not stripped:
            continue
        units.extend(_split_long(stripped, cfg.max_chars))
    return units


def _split_long(paragraph: str, limit: int) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]

    pieces: list[str] = []
    buffer = ""
    for sentence in _SENTENCE.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(buffer) + len(sentence) + 1 <= limit:
            buffer = f"{buffer} {sentence}".strip()
            continue
        if buffer:
            pieces.append(buffer)
        # 문장 하나가 상한을 넘으면(표·목록이 한 줄로 붙은 경우) 강제로 자른다.
        while len(sentence) > limit:
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _with_overlap(previous: str, unit: str, cfg: ChunkingConfig) -> str:
    """새 청크 앞에 직전 청크의 꼬리를 덧붙인다.

    조항 머리로 시작하는 유닛은 겹치지 않는다 — 조항 경계는 의미가 확실히
    끊기는 지점이라 앞 조항을 끌고 오면 오히려 오답의 근거가 된다.
    겹쳐서 상한을 넘게 되는 경우도 포기한다. 상한은 임베딩 모델 입력 길이라
    넘기면 뒤가 잘려 나가므로, 겹침보다 우선한다.
    """
    if cfg.overlap_chars <= 0 or _ARTICLE_HEAD.match(unit):
        return unit
    room = cfg.max_chars - len(unit) - 1
    if room <= 0:
        return unit
    tail = previous[-min(cfg.overlap_chars, room) :].strip()
    return f"{tail}\n{unit}" if tail else unit


def _merge_runts(chunks: list[str], cfg: ChunkingConfig) -> list[str]:
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < cfg.min_chars:
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)
    return merged
