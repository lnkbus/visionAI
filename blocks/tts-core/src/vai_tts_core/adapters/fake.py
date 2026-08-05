"""결정적 가짜 합성기 — GPU 없는 CI에서 파이프라인 전체를 검증한다.

모든 AI 엔진 블록은 fake 어댑터를 제공한다(docs/06 블록 개발 규칙 6). 없으면
GPU가 있는 곳에서만 통합 테스트가 돌고, 결국 아무도 안 돌린다.

**글자 수에 비례한 길이의 오디오**를 낸다. 그래야 "합성 시간이 텍스트 길이에
비례하는가", "끼어들기가 중간에 끊는가" 같은 것을 실제로 검증할 수 있다.
무음을 돌려주면 파이프라인은 통과하지만 아무것도 확인되지 않는다.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator
from typing import Any

from vai_contracts.speech import VoiceProfile
from vai_tts_core.adapters.base import BaseTTSAdapter

MS_PER_CHAR = 90
"""글자당 발화 길이. 한국어 상담 속도(분당 300~350음절)에 대략 맞춘다."""

CHUNK_MS = 40
"""조각 길이. 짧을수록 첫 소리가 빨라지지만 이벤트 수가 늘어난다.
40ms는 RTP 패킷(20ms) 두 개에 대응해 전화망으로 흘리기 좋다."""


class FakeTTSAdapter(BaseTTSAdapter):
    """텍스트 길이에 비례하는 톤을 낸다. 글자마다 음높이가 달라져 육안(파형)
    확인과 회귀 비교가 가능하다."""

    name = "fake"
    native_sample_rate = 24000

    def __init__(self) -> None:
        self._latency_ms = 0.0

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        # 실제 엔진의 추론 지연을 흉내 내 지연 예산 테스트를 의미 있게 만든다.
        self._latency_ms = float(config.get("latency_ms", 0))
        self.native_sample_rate = int(config.get("sample_rate", self.native_sample_rate))

    async def synthesize_stream(
        self, text: str, voice: VoiceProfile
    ) -> AsyncGenerator[bytes, None]:
        if not text.strip():
            return

        total_ms = max(CHUNK_MS, int(len(text) * MS_PER_CHAR / max(voice.speed, 0.1)))
        samples_per_chunk = int(self.native_sample_rate * CHUNK_MS / 1000)
        chunks = math.ceil(total_ms / CHUNK_MS)

        # 글자에서 주파수를 뽑는다. 같은 텍스트는 항상 같은 파형을 낸다 —
        # 결정적이어야 회귀 테스트가 성립한다.
        base_hz = 110 + (sum(ord(c) for c in text) % 8) * 20

        phase = 0.0
        for index in range(chunks):
            if self._latency_ms:
                await asyncio.sleep(self._latency_ms / 1000)
            hz = base_hz * (1 + 0.05 * (index % 5)) * (2 ** (voice.pitch / 12))
            step = 2 * math.pi * hz / self.native_sample_rate
            frame = bytearray()
            for _ in range(samples_per_chunk):
                frame += int(12000 * math.sin(phase)).to_bytes(2, "little", signed=True)
                phase += step
            yield bytes(frame)
