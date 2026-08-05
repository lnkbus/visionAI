"""화자분리 테스트.

화자를 잘못 가르면 회의록의 "누가 무엇을 말했는가"가 통째로 틀린다.
그런데 틀려도 오류가 나지 않고 그럴듯한 회의록이 나오므로, 경계 조건을
테스트로 고정하는 것 외에 방법이 없다.
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from vai_spk_dia.diarizer import DiarizerConfig, OnlineDiarizer
from vai_spk_dia.embedding import SpectralEmbedder, create_embedder

RATE = 16000


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def diarizer(**overrides: object) -> OnlineDiarizer:
    return OnlineDiarizer(config=DiarizerConfig(**overrides))  # type: ignore[arg-type]


def voice(freq: int, ms: int = 1500, *, harmonics: int = 4, amplitude: int = 9000) -> bytes:
    """기본 주파수와 배음이 다른 합성 음성. 화자 차이를 흉내 낸다."""
    count = int(RATE * ms / 1000)
    samples = []
    for n in range(count):
        value = sum(
            (amplitude / (h + 1)) * math.sin(2 * math.pi * freq * (h + 1) * n / RATE)
            for h in range(harmonics)
        )
        samples.append(max(-32000, min(32000, int(value))))
    return struct.pack(f"<{count}h", *samples)


# ── 군집화 로직 ──────────────────────────────────────────────────────────────


def test_first_utterance_creates_a_speaker() -> None:
    result = diarizer().assign(unit(1, 0, 0), 2000)

    assert result is not None
    assert result.speaker_id == "speaker_1"
    assert result.is_new


def test_similar_embedding_joins_the_same_speaker() -> None:
    dia = diarizer(similarity_threshold=0.8)
    dia.assign(unit(1, 0, 0), 2000)

    result = dia.assign(unit(0.98, 0.2, 0), 2000)

    assert result is not None
    assert result.speaker_id == "speaker_1"
    assert not result.is_new
    assert dia.speaker_count == 1


def test_distant_embedding_creates_a_new_speaker() -> None:
    dia = diarizer(similarity_threshold=0.8)
    dia.assign(unit(1, 0, 0), 2000)

    result = dia.assign(unit(0, 1, 0), 2000)

    assert result is not None
    assert result.speaker_id == "speaker_2"
    assert result.is_new
    assert dia.speaker_count == 2


def test_backchannel_does_not_create_a_participant() -> None:
    """ "네", "음" 같은 맞장구가 매번 새 참석자가 되면 회의록에 20명이 잡힌다."""
    dia = diarizer(similarity_threshold=0.9, min_duration_ms=700)
    dia.assign(unit(1, 0, 0), 2000)

    result = dia.assign(unit(0, 1, 0), 300)  # 300ms — 임계 미만

    assert result is not None
    assert result.speaker_id == "speaker_1", "짧은 발화는 기존 화자에 붙는다"
    assert dia.speaker_count == 1


def test_short_utterance_before_any_speaker_is_undecided() -> None:
    """붙일 화자도 없고 새로 만들 수도 없으면 판정을 미룬다."""
    assert diarizer(min_duration_ms=700).assign(unit(1, 0, 0), 200) is None


def test_speaker_cap_folds_into_the_nearest() -> None:
    """회의에 20명이 잡히면 분리가 실패한 것이지 실제로 20명인 경우는 드물다."""
    dia = diarizer(similarity_threshold=0.99, max_speakers=2)
    dia.assign(unit(1, 0, 0), 2000)
    dia.assign(unit(0, 1, 0), 2000)

    result = dia.assign(unit(0, 0, 1), 2000)

    assert result is not None
    assert not result.is_new
    assert dia.speaker_count == 2


def test_centroid_drifts_toward_recent_speech() -> None:
    """목소리는 회의 중에도 변한다(피로·거리). 중심이 고착되면 후반부를 놓친다."""
    dia = diarizer(similarity_threshold=0.5, centroid_momentum=0.5)
    dia.assign(unit(1, 0, 0), 2000)
    before = dia.speakers[0].centroid.copy()

    dia.assign(unit(0.7, 0.7, 0), 2000)

    assert not np.allclose(before, dia.speakers[0].centroid)
    assert abs(float(np.linalg.norm(dia.speakers[0].centroid)) - 1.0) < 1e-5


def test_summary_orders_by_speaking_time() -> None:
    """회의록 참석자 목록은 발언량 순이 유용하다."""
    dia = diarizer(similarity_threshold=0.9)
    dia.assign(unit(1, 0, 0), 1000)
    dia.assign(unit(0, 1, 0), 5000)

    summary = dia.summary()

    assert summary[0]["speaker_id"] == "speaker_2"
    assert summary[0]["total_ms"] == 5000


def test_empty_embedding_is_rejected() -> None:
    assert diarizer().assign(np.array([], dtype=np.float32), 2000) is None


# ── 스펙트럼 임베더 ──────────────────────────────────────────────────────────


@pytest.fixture
async def embedder() -> SpectralEmbedder:
    embedder = SpectralEmbedder()
    await embedder.initialize("", {})
    return embedder


async def test_embedding_is_normalized(embedder: SpectralEmbedder) -> None:
    vector = await embedder.embed(voice(180), RATE)

    assert vector is not None
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5


async def test_short_segment_yields_no_embedding(embedder: SpectralEmbedder) -> None:
    """근거 없는 라벨을 붙이면 회의록에 없는 참석자가 생긴다."""
    assert await embedder.embed(b"\x00\x00" * 100, RATE) is None


async def test_silence_yields_no_embedding(embedder: SpectralEmbedder) -> None:
    assert await embedder.embed(b"\x00\x00" * 8000, RATE) is None


async def test_same_voice_is_more_similar_than_different_voice(
    embedder: SpectralEmbedder,
) -> None:
    """임베더가 최소한의 변별력을 갖는지 — 이게 안 되면 군집화가 무의미하다."""
    low_a = await embedder.embed(voice(110), RATE)
    low_b = await embedder.embed(voice(112), RATE)
    high = await embedder.embed(voice(260, harmonics=6), RATE)

    assert low_a is not None and low_b is not None and high is not None
    same = float(low_a @ low_b)
    different = float(low_a @ high)

    assert same > different, (
        f"같은 목소리({same:.3f})가 다른 목소리({different:.3f})보다 가까워야 한다"
    )


async def test_registry_exposes_both_tiers() -> None:
    """CI용 스펙트럼과 운영용 신경망이 같은 인터페이스 뒤에 있어야 한다."""
    for name in ("spectral", "speechbrain"):
        assert create_embedder(name).name


def test_registry_rejects_unknown_embedder() -> None:
    with pytest.raises(ValueError, match="spectral"):
        create_embedder("nope")
