"""통합 테스트 픽스처.

Redis·GPU 없이 파이프라인 전체를 돌리기 위해 인메모리 버스와 fake 어댑터를 쓴다.
docs/06 §2-6의 "fake 어댑터 필수" 규칙이 여기서 값을 한다.
"""

from __future__ import annotations

import math
import struct

import pytest

from vai_common.bus import InMemoryEventBus

SAMPLE_RATE = 16000


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


def pcm_tone(duration_ms: int, *, freq: int = 220, amplitude: int = 12000) -> bytes:
    """음성처럼 에너지가 있는 PCM 16bit 톤."""
    count = int(SAMPLE_RATE * duration_ms / 1000)
    return struct.pack(
        f"<{count}h",
        *(int(amplitude * math.sin(2 * math.pi * freq * n / SAMPLE_RATE)) for n in range(count)),
    )


def pcm_silence(duration_ms: int) -> bytes:
    count = int(SAMPLE_RATE * duration_ms / 1000)
    return b"\x00\x00" * count
