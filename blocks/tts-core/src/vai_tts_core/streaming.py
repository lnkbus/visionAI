"""문장 분할과 재표본화 — 첫 소리를 앞당기는 두 가지 장치.

**문장 분할**: 답변 전체를 한 번에 합성하면 첫 소리가 문장 수만큼 늦어진다.
"확인해 드리겠습니다. 잠시만 기다려 주세요. 조회 결과..."를 통째로 합성하면
2초가 지나서야 첫 소리가 나고, 그 사이 고객은 "여보세요?"를 두 번 한다.
첫 문장만 합성해 바로 내보내고 나머지는 뒤따라 보낸다.

**첫 문장은 짧게 자른다.** 첫 문장이 길면 분할의 의미가 없다. 쉼표에서도
끊어 첫 조각을 최대한 짧게 만든다 — 두 번째 조각부터는 재생 중에 합성되므로
길어도 상관없다.
"""

from __future__ import annotations

import re

ARTICLE_MARKERS = "0-9조항호장절목"
"""이 글자 뒤의 마침표는 문장 끝이 아니다. "제1조. 목적"에서 끊으면 TTS가
조항 번호마다 어색하게 멈추고, 약관 안내에서 바로 드러난다."""

SENTENCE_END = re.compile(
    # 물음표·느낌표는 문장 끝이 확실하다.
    r"(?<=[!?。？！])\s+"
    # 한국어 종결어미. 마침표를 남겨 두어 TTS가 문장 끝임을 알아채게 한다.
    r"|(?<=[다요]\.)\s*"
    # 일반 마침표 — 조항 번호·소수점 뒤는 제외한다.
    rf"|(?<![{ARTICLE_MARKERS}]\.)(?<=\.)\s+"
    r"|\n+"
)
"""문장 경계."""

CLAUSE_BREAK = re.compile(r"(?<=[,、])\s*|(?<=니다)\s+(?=[가-힣])")

FIRST_CHUNK_MAX = 24
"""첫 조각의 최대 글자 수. 넘으면 쉼표·절 경계에서 더 자른다.
첫 소리가 늦으면 뒤가 아무리 빨라도 대화가 무너진다."""

MIN_CHUNK = 4
"""이보다 짧은 조각은 앞뒤에 붙인다. "네." 하나만 따로 합성하면
엔진 호출 비용이 발화 길이보다 크고, 조각 사이 이음매가 들린다."""


def split_sentences(text: str) -> list[str]:
    """문장 단위로 나눈다."""
    parts = [part.strip() for part in SENTENCE_END.split(text) if part and part.strip()]
    if not parts:
        return []

    merged: list[str] = []
    for part in parts:
        # 너무 짧은 조각은 앞에 붙인다. 이음매가 들리는 것을 막는다.
        if merged and len(part) < MIN_CHUNK:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def split_for_streaming(text: str) -> list[str]:
    """스트리밍용 분할. **첫 조각을 가장 짧게** 만든다."""
    sentences = split_sentences(text)
    if not sentences:
        return []

    head, *rest = sentences
    if len(head) <= FIRST_CHUNK_MAX:
        return [head, *rest]

    # 첫 문장이 길면 절 경계에서 한 번 더 자른다.
    clauses = [c.strip() for c in CLAUSE_BREAK.split(head) if c and c.strip()]
    if len(clauses) > 1 and len(clauses[0]) >= MIN_CHUNK:
        return [clauses[0], " ".join(clauses[1:]), *rest]
    return [head, *rest]


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """선형 보간 재표본화.

    합성은 엔진의 기본 표본율로 하고 여기서 낮춘다. 엔진에게 8kHz로 직접
    합성시키면 음질이 눈에 띄게 나빠진다 — 전화망으로 나가더라도 마찬가지다.

    선형 보간은 고품질 리샘플러가 아니다. 전화 대역(8kHz)으로 **내리는**
    용도라 실용상 충분하지만, 스튜디오 품질이 요구되면 polyphase 필터로 바꾼다.
    """
    if source_rate == target_rate or not pcm:
        return pcm

    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size == 0:
        return pcm

    target_length = max(1, round(samples.size * target_rate / source_rate))
    positions = np.linspace(0, samples.size - 1, target_length)
    resampled = np.interp(positions, np.arange(samples.size), samples.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()


def duration_ms(pcm: bytes, sample_rate: int) -> int:
    return int(len(pcm) / 2 / sample_rate * 1000) if sample_rate else 0
