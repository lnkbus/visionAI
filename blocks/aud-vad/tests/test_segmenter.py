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


# ── 잡음 필터 (기본 설정에서 실제로 동작하는가) ─────────────────────────────


def test_cough_is_dropped_with_default_settings() -> None:
    """기본 설정에서 걸러지지 않으면 그 필터는 없는 것과 같다.

    구간 길이로 재면 패딩(200ms)+행오버(400ms) 때문에 50ms 기침도 660ms가
    되어 통과한다. 그러면 기침 하나가 STT·필터·검색 파이프라인 전체를 태운다.
    """
    segmenter = make()  # 기본값 그대로

    segmenter.push(KEY, silence(300))
    segmenter.push(KEY, tone(50))
    finals = [s for s in segmenter.push(KEY, silence(800)) if s.is_final]

    assert finals, "구간 자체는 만들어진다"
    assert not any(segmenter.is_meaningful(s) for s in finals)


def test_short_backchannel_survives() -> None:
    """ "네"는 250ms쯤이다. 잡음을 거르려다 맞장구를 잃으면 음성봇이 답을 못 받는다."""
    segmenter = make()

    segmenter.push(KEY, silence(300))
    segmenter.push(KEY, tone(250))
    finals = [s for s in segmenter.push(KEY, silence(800)) if s.is_final]

    assert any(segmenter.is_meaningful(s) for s in finals)


def test_speech_ms_excludes_padding_and_hangover() -> None:
    segmenter = make()

    segmenter.push(KEY, silence(400))
    segmenter.push(KEY, tone(600))
    final = next(s for s in segmenter.push(KEY, silence(800)) if s.is_final)

    assert final.duration_ms > final.speech_ms, "구간에는 패딩·행오버가 붙는다"
    assert 550 <= final.speech_ms <= 700, f"음성 길이가 실제와 어긋난다: {final.speech_ms}"


def test_interim_also_carries_speech_length() -> None:
    """중간 구간에도 실려야 워커가 같은 기준으로 거를 수 있다."""
    segmenter = make(interim_interval_ms=300, hangover_ms=400)

    interims = [s for s in segmenter.push(KEY, tone(1200)) if not s.is_final]

    assert interims
    assert all(s.speech_ms > 0 for s in interims)


# ── 경계 동작 ────────────────────────────────────────────────────────────────


def test_pause_inside_a_sentence_does_not_split_it() -> None:
    """문장 중간의 쉼에서 끊기면 STT가 조각난 문장을 받는다."""
    segmenter = make(hangover_ms=500, interim_interval_ms=10_000)

    segments = segmenter.push(KEY, tone(400) + silence(200) + tone(400))

    assert not [s for s in segments if s.is_final], "행오버 안의 쉼은 발화를 끊지 않는다"


def test_speech_continues_after_a_forced_cut() -> None:
    """상한으로 끊은 뒤에도 말은 이어진다. 그 뒤 오디오를 잃으면 문장이 사라진다."""
    segmenter = make(max_segment_ms=400, interim_interval_ms=10_000, hangover_ms=300)

    segments = segmenter.push(KEY, tone(1500))
    finals = [s for s in segments if s.is_final]

    assert len(finals) >= 2, "이어지는 발화가 새 구간으로 계속 잡혀야 한다"
    assert all(s.speech_ms > 0 for s in finals)


def test_state_resets_between_utterances() -> None:
    """앞 발화의 음성 길이가 다음 발화에 누적되면 잡음 필터가 무력해진다."""
    segmenter = make(hangover_ms=300, interim_interval_ms=10_000)

    segmenter.push(KEY, tone(800))
    first = next(s for s in segmenter.push(KEY, silence(500)) if s.is_final)
    segmenter.push(KEY, tone(50))
    second = next(s for s in segmenter.push(KEY, silence(500)) if s.is_final)

    assert first.speech_ms > 700
    assert second.speech_ms < 200, "이전 발화 길이가 새어 들어왔다"


def test_flushed_segment_reports_speech_length() -> None:
    segmenter = make(hangover_ms=1000, interim_interval_ms=10_000)
    segmenter.push(KEY, tone(500))

    leftover = segmenter.flush(KEY)

    assert leftover is not None
    assert leftover.speech_ms >= 400


def test_is_speaking_tracks_the_live_state() -> None:
    """끼어들기는 구간 확정을 기다리지 않는다 — 말이 시작된 그 순간을 봐야 한다."""
    segmenter = make(hangover_ms=300, interim_interval_ms=10_000)

    assert not segmenter.is_speaking(KEY)
    segmenter.push(KEY, tone(300))
    assert segmenter.is_speaking(KEY)
    segmenter.push(KEY, silence(600))
    assert not segmenter.is_speaking(KEY)


def test_dropped_stream_starts_clean() -> None:
    segmenter = make(hangover_ms=300, interim_interval_ms=10_000)
    segmenter.push(KEY, tone(400))

    segmenter.drop(KEY)

    assert not segmenter.is_speaking(KEY)
    assert segmenter.push(KEY, silence(600)) == []
