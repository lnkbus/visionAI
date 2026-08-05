"""합성 오케스트레이션 — 정규화 → 분할 → 합성 → 재표본화, 그리고 끼어들기.

버스·HTTP와 무관한 순수 로직으로 분리했다. 여기서 검증할 것은 두 가지다:

1. **첫 소리까지의 지연(TTFB)** — 첫 조각이 전체 합성을 기다리지 않는가
2. **끼어들기** — 취소가 들어온 뒤 오디오가 한 조각도 더 나가지 않는가

두 번째가 특히 중요하다. 취소했는데 조각 하나가 더 나가면 고객 귀에는 봇이
말을 끊지 않은 것으로 들린다. "거의 멈췄다"는 멈춘 것이 아니다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

from vai_contracts.speech import AudioEncoding, VoiceProfile
from vai_tts_core.adapters.base import BaseTTSAdapter
from vai_tts_core.normalize import normalize
from vai_tts_core.streaming import duration_ms, resample_pcm16, split_for_streaming

log = logging.getLogger(__name__)

TTFB_BUDGET_MS = 500.0
"""첫 소리까지의 예산. 사람은 0.5초 안에 반응이 없으면 "안 들렸나?" 하고
다시 말하기 시작한다 — 그 순간 두 사람이 동시에 말하는 상태가 된다."""


@dataclass
class Segment:
    """합성된 조각 하나."""

    seq: int
    pcm: bytes
    text: str
    sample_rate: int
    encoding: AudioEncoding
    duration_ms: int
    is_final: bool = False


@dataclass
class TurnStats:
    ttfb_ms: float = 0.0
    total_ms: float = 0.0
    audio_ms: int = 0
    segments: int = 0
    cancelled: bool = False


class CancellationRegistry:
    """턴별 취소 신호.

    **턴 단위로 나누는 이유**: 세션 단위로만 묶으면 끼어들기가 다음 턴의
    오디오까지 함께 버린다. 고객이 말을 끊고 봇이 새로 답하려는 순간
    그 답까지 사라지는 셈이다.
    """

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], asyncio.Event] = {}

    def token(self, session_id: str, turn_id: str) -> asyncio.Event:
        return self._events.setdefault((session_id, turn_id), asyncio.Event())

    def cancel(self, session_id: str, turn_id: str = "") -> int:
        """턴 하나 또는 세션 전체를 취소한다. 취소된 턴 수를 돌려준다."""
        targets = [
            key
            for key in self._events
            if key[0] == session_id and (not turn_id or key[1] == turn_id)
        ]
        for key in targets:
            self._events[key].set()
        return len(targets)

    def release(self, session_id: str, turn_id: str) -> None:
        """턴이 끝나면 정리한다. 안 지우면 긴 통화에서 계속 쌓인다."""
        self._events.pop((session_id, turn_id), None)

    @property
    def active(self) -> int:
        return len(self._events)


@dataclass
class Synthesizer:
    """어댑터를 감싸 정규화·분할·재표본화·취소를 얹는다."""

    adapter: BaseTTSAdapter
    registry: CancellationRegistry = field(default_factory=CancellationRegistry)

    async def synthesize(
        self,
        session_id: str,
        turn_id: str,
        text: str,
        voice: VoiceProfile,
        *,
        readings: Mapping[str, str] | None = None,
    ) -> AsyncIterator[tuple[Segment, TurnStats]]:
        """조각을 순서대로 흘린다. 취소되면 즉시 멈춘다."""
        stats = TurnStats()
        cancel = self.registry.token(session_id, turn_id)
        started = time.perf_counter()

        try:
            chunks = split_for_streaming(normalize(text, readings=readings))
            if not chunks:
                return

            seq = 0
            for chunk in chunks:
                if cancel.is_set():
                    stats.cancelled = True
                    break

                async for pcm in self.adapter.synthesize_stream(chunk, voice):
                    # 조각을 내보내기 **직전에** 다시 본다. 어댑터가 여러 조각을
                    # 흘리는 동안 취소가 들어올 수 있고, 그때 이미 만든 것을
                    # 내보내면 봇이 말을 끊지 않은 것으로 들린다.
                    if cancel.is_set():
                        stats.cancelled = True
                        break

                    audio = self._convert(pcm, voice)
                    if not audio:
                        continue
                    if seq == 0:
                        stats.ttfb_ms = (time.perf_counter() - started) * 1000
                        if stats.ttfb_ms > TTFB_BUDGET_MS:
                            log.warning(
                                "첫 소리 지연 예산 초과",
                                extra={
                                    "session_id": session_id,
                                    "ttfb_ms": round(stats.ttfb_ms, 1),
                                    "budget_ms": TTFB_BUDGET_MS,
                                    "chars": len(text),
                                },
                            )

                    length = duration_ms(audio, voice.sample_rate)
                    stats.audio_ms += length
                    stats.segments += 1
                    segment = Segment(
                        seq=seq,
                        pcm=audio,
                        text=chunk,
                        sample_rate=voice.sample_rate,
                        encoding=voice.encoding,
                        duration_ms=length,
                        is_final=False,
                    )
                    seq += 1
                    yield segment, stats

                if stats.cancelled:
                    break

            if not stats.cancelled and seq:
                # 마지막을 알리는 빈 조각. 재생부가 "이제 끝났다"를 알아야
                # 다음 턴의 마이크를 열 수 있다.
                stats.total_ms = (time.perf_counter() - started) * 1000
                yield (
                    Segment(
                        seq=seq,
                        pcm=b"",
                        text="",
                        sample_rate=voice.sample_rate,
                        encoding=voice.encoding,
                        duration_ms=0,
                        is_final=True,
                    ),
                    stats,
                )
        finally:
            stats.total_ms = stats.total_ms or (time.perf_counter() - started) * 1000
            self.registry.release(session_id, turn_id)

    def _convert(self, pcm: bytes, voice: VoiceProfile) -> bytes:
        audio = resample_pcm16(pcm, self.adapter.native_sample_rate, voice.sample_rate)
        if voice.encoding is AudioEncoding.MULAW:
            audio = _to_mulaw(audio)
        return audio


_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def _to_mulaw(pcm: bytes) -> bytes:
    """PCM16 → G.711 μ-law.

    전화망으로 나갈 때 필요하다. AUD-RTP의 디코더와 **짝을 이루어야** 하므로
    같은 표(ITU-T G.711)를 쓴다 — 한쪽만 바꾸면 잡음만 나간다.
    """
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0).astype(np.uint8)
    magnitude = np.minimum(np.abs(samples), _MULAW_CLIP) + _MULAW_BIAS

    exponent = np.zeros_like(magnitude)
    for shift in range(7, 0, -1):
        exponent = np.where((magnitude >> (shift + 7)) & 1, shift, exponent)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    encoded = ~(sign | (exponent << 4).astype(np.uint8) | mantissa.astype(np.uint8))
    result: bytes = encoded.astype(np.uint8).tobytes()
    return result
