"""발화 분할기 단위 테스트.

발화 경계 판정은 응답 지연에 직결되므로 오디오 픽스처로 직접 검증한다.
"""

from __future__ import annotations

import math
import struct

from vai_aud_vad.adapters.energy import EnergyVadAdapter
from vai_aud_vad.segmenter import SegmenterConfig, SpeechSegmenter

RATE = 16000
KEY = "sess:customer"


def tone(duration_ms: int, *, amplitude: int = 12000, freq: int = 220) -> bytes:
    count = int(RATE * duration_ms / 1000)
    return struct.pack(
        f"<{count}h",
        *(int(amplitude * math.sin(2 * math.pi * freq * n / RATE)) for n in range(count)),
    )


def silence(duration_ms: int) -> bytes:
    return b"\x00\x00" * int(RATE * duration_ms / 1000)


def make(**overrides: object) -> SpeechSegmenter:
    config = SegmenterConfig(**overrides)  # type: ignore[arg-type]
    return SpeechSegmenter(EnergyVadAdapter(), RATE, config)


def test_silence_alone_produces_nothing() -> None:
    segmenter = make()
    assert segmenter.push(KEY, silence(1000)) == []


def test_speech_then_silence_emits_final_segment() -> None:
    segmenter = make(hangover_ms=300, interim_interval_ms=10_000)

    assert all(not s.is_final for s in segmenter.push(KEY, tone(500)))
    segments = segmenter.push(KEY, silence(600))

    finals = [s for s in segments if s.is_final]
    assert len(finals) == 1
    # 앞뒤로 패딩·행오버가 붙으므로 발화 길이보다 길어야 정상이다.
    assert finals[0].duration_ms >= 500


def test_long_speech_emits_interim_before_finishing() -> None:
    """발화가 끝날 때까지 기다리면 1초 예산을 지킬 수 없다."""
    segmenter = make(interim_interval_ms=400, hangover_ms=300)

    segments = segmenter.push(KEY, tone(1500))

    assert [s for s in segments if not s.is_final], "중간 구간이 나와야 한다"
    assert all(not s.is_final for s in segments), "무음 없이는 확정되면 안 된다"


def test_max_segment_forces_a_cut() -> None:
    """무한 발화에서 STT 입력이 계속 커지는 것을 막는다."""
    segmenter = make(max_segment_ms=600, interim_interval_ms=10_000, hangover_ms=300)

    segments = segmenter.push(KEY, tone(2000))

    assert [s for s in segments if s.is_final], "상한을 넘으면 강제로 끊겨야 한다"


def test_short_blip_is_not_meaningful() -> None:
    segmenter = make(hangover_ms=200, min_segment_ms=300, interim_interval_ms=10_000)

    segmenter.push(KEY, tone(60))
    segments = segmenter.push(KEY, silence(400))

    finals = [s for s in segments if s.is_final]
    assert finals, "구간 자체는 만들어진다"
    assert not any(segmenter.is_meaningful(s) for s in finals), "짧은 잡음은 걸러져야 한다"


def test_channels_do_not_share_state() -> None:
    """고객/상담원 채널이 서로의 버퍼를 오염시키면 자막이 뒤섞인다."""
    segmenter = make(hangover_ms=300, interim_interval_ms=10_000)

    segmenter.push("sess:customer", tone(400))
    agent_segments = segmenter.push("sess:agent", silence(600))

    assert agent_segments == [], "다른 채널의 발화가 넘어오면 안 된다"


def test_flush_recovers_speech_cut_off_by_hangup() -> None:
    segmenter = make(hangover_ms=1000, interim_interval_ms=10_000)
    segmenter.push(KEY, tone(500))

    leftover = segmenter.flush(KEY)

    assert leftover is not None and leftover.is_final
    assert segmenter.flush(KEY) is None, "flush는 스트림을 제거해야 한다"


def test_energy_vad_adapts_to_background_noise() -> None:
    """고정 임계값이면 통화별 입력 게인 차이를 못 넘긴다."""
    adapter = EnergyVadAdapter()
    frame_bytes = adapter.frame_samples(RATE) * 2

    quiet = tone(200, amplitude=300)
    for offset in range(0, len(quiet) - frame_bytes, frame_bytes):
        adapter.is_speech(quiet[offset : offset + frame_bytes], RATE)

    loud = tone(60, amplitude=15000)
    assert adapter.is_speech(loud[:frame_bytes], RATE)
