"""마이크가 소리를 담고 있는가 — 로그가 말해 주는가.

맥 시연에서 자막이 한 줄도 안 나왔다. STT 로그에는 이것만 반복됐다:

    인식 결과를 버렸다 · reason='무음 상용구 (무음 0.00 · 근거 -0.79)' text=감사합니다.

10분 동안 STT 가 일곱 번 불렸다. 말하는 동안이라면 몇 초마다 불려야 한다.
**마이크가 목소리를 안 담고 있었다는 뜻**인데, 그것을 알 방법이 없었다.
화면에는 "자막이 안 나온다"로만 보이고, 원인이 마이크인지 인식인지 필터인지
가릴 근거가 어디에도 없었다.

숫자 하나면 갈린다. 사람 목소리를 보통 거리에서 받으면 RMS 가 1000~5000
대다. 300 아래는 방 소음이나 무음에 가깝고, 그런 구간을 받은 Whisper 는
학습 데이터의 상용구를 지어낸다.

**세션당 한 번만 남긴다.** 구간마다 찍으면 로그가 못 쓰게 되고, 못 쓰는
로그는 없는 로그와 같다.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from vai_aud_vad.worker import LOW_LEVEL_RMS, VadWorker, _level

RATE = 16000


def _pcm(amplitude: int, seconds: float = 1.0) -> bytes:
    count = int(RATE * seconds)
    return (np.sin(np.arange(count) * 0.07) * amplitude).astype("<i2").tobytes()


class _Segment:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.speech_ms = 900


class _Bus:
    async def publish(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
        return None


def _worker() -> VadWorker:
    from vai_aud_vad.adapters.energy import EnergyVadAdapter

    return VadWorker(_Bus(), EnergyVadAdapter(), group="g", consumer="c")  # type: ignore[arg-type]


def test_소리_크기를_잰다() -> None:
    quiet_rms, quiet_peak = _level(_pcm(80))
    loud_rms, loud_peak = _level(_pcm(9000))

    assert quiet_rms < loud_rms and quiet_peak < loud_peak
    assert loud_rms > LOW_LEVEL_RMS > quiet_rms, (
        f"기준값({LOW_LEVEL_RMS})이 목소리와 잡음 사이에 있지 않다"
    )


def test_빈_구간에도_안_죽는다() -> None:
    assert _level(b"") == (0.0, 0)


def test_너무_작으면_무엇을_확인할지_말한다(caplog: pytest.LogCaptureFixture) -> None:
    """"소리가 작다"만 적으면 다음에 뭘 할지 모른다."""
    with caplog.at_level(logging.WARNING):
        _worker()._report_level("s-1", _Segment(_pcm(80)))  # type: ignore[arg-type]

    assert caplog.records, "조용한데 아무 말도 안 한다"
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "마이크" in record.getMessage()
    assert getattr(record, "확인", ""), "무엇을 확인하라는지 없다"
    assert getattr(record, "rms", None) is not None, "근거 숫자가 없으면 판단할 수 없다"


def test_충분히_크면_확인만_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    """경고만 시험하면 "늘 경고하는" 구성을 통과시킨다."""
    with caplog.at_level(logging.INFO):
        _worker()._report_level("s-2", _Segment(_pcm(9000)))  # type: ignore[arg-type]

    assert caplog.records
    assert caplog.records[0].levelno == logging.INFO
    assert "마이크 입력 확인" in caplog.records[0].getMessage()


def test_세션당_한_번만_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    """구간마다 찍으면 로그가 못 쓰게 되고, 못 쓰는 로그는 없는 로그와 같다."""
    worker = _worker()

    with caplog.at_level(logging.INFO):
        for _ in range(5):
            worker._report_level("s-3", _Segment(_pcm(9000)))  # type: ignore[arg-type]

    assert len(caplog.records) == 1, f"{len(caplog.records)}줄이 나왔다"


def test_세션이_다르면_각각_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    worker = _worker()

    with caplog.at_level(logging.INFO):
        worker._report_level("s-4", _Segment(_pcm(9000)))  # type: ignore[arg-type]
        worker._report_level("s-5", _Segment(_pcm(80)))  # type: ignore[arg-type]

    assert len(caplog.records) == 2
