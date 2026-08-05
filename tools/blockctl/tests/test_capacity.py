"""용량 산정 — 견적과 계약에 들어가는 숫자다."""

from __future__ import annotations

import pytest

from blockctl.capacity import CHANNELS_PER_SEAT, license_channels, plan_capacity


def test_상담석_하나는_두_채널이다() -> None:
    """이 계산에서 가장 흔하고 가장 비싼 실수다.

    1로 잡으면 도입 직후 절반이 인식되지 않고, 증상은 "가끔 상담사 말이
    안 잡힌다"로 나타나 원인을 찾기 어렵다.
    """
    assert CHANNELS_PER_SEAT == 2
    plan = plan_capacity(100, concurrency=1.0)
    assert plan.channels == 200


def test_동시_통화율이_반영된다() -> None:
    assert plan_capacity(1000, concurrency=0.5).channels == 1000
    assert plan_capacity(1000, concurrency=0.75).channels == 1500


def test_gpu는_올림한다() -> None:
    """0.1장이라는 것은 없다. 내림하면 피크에서 막힌다."""
    plan = plan_capacity(1, concurrency=1.0, with_diarization=False)
    assert plan.requirements[0].gpus == 1


@pytest.mark.parametrize("seats", [0, -5])
def test_말이_안_되는_입력은_거부한다(seats: int) -> None:
    with pytest.raises(ValueError, match="1 이상"):
        plan_capacity(seats)


def test_동시율_범위를_지킨다() -> None:
    with pytest.raises(ValueError, match="0 초과"):
        plan_capacity(10, concurrency=1.5)


def test_cpu_프로파일은_규모를_경고한다() -> None:
    """CPU 추론으로 상담센터 규모를 견적하면 그 견적이 계약이 된다."""
    plan = plan_capacity(1000, stt_profile="stt-small-cpu")
    assert any("CPU 추론" in w for w in plan.warnings)


def test_대규모는_단일노드_한계를_말한다() -> None:
    assert any("단일 노드" in w for w in plan_capacity(1000).warnings)
    assert not any("단일 노드" in w for w in plan_capacity(50).warnings)


def test_숫자가_실측이_아님을_항상_밝힌다() -> None:
    """가정을 실측으로 읽으면 그 숫자가 계약서에 들어간다."""
    for seats in (10, 1000, 5000):
        assert any("설계 가정" in w for w in plan_capacity(seats).warnings)


def test_라이선스_채널과_계산이_같은_숫자를_쓴다() -> None:
    """둘이 갈리면 피크에 429가 나거나 초과 사용이 청구되지 않는다."""
    assert license_channels(1000) == plan_capacity(1000).channels


def test_tts는_채널의_절반으로_잡는다() -> None:
    plan = plan_capacity(1000, with_tts=True)
    tts = next(r for r in plan.requirements if r.engine == "TTS")
    stt = next(r for r in plan.requirements if r.engine == "STT")
    assert tts.channels == stt.channels // 2
    assert "절반" in tts.note, "가정을 적지 않으면 실측으로 읽힌다"


def test_알_수_없는_프로파일은_가능한_값을_알려준다() -> None:
    with pytest.raises(ValueError, match="가능"):
        plan_capacity(10, stt_profile="없는것")
