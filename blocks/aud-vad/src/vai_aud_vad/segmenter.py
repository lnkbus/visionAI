"""발화 구간 분할기 — AUD-VAD의 핵심 로직.

버스·네트워크와 무관한 순수 상태 기계로 분리했다. 발화 경계 판정은 지연시간
예산에 직접 영향을 주는 부분이라 오디오 픽스처로 단위 테스트가 되어야 한다.

동작:

* 프레임을 VAD에 넣어 음성/무음을 판정한다.
* ``speech_pad_ms``만큼의 직전 무음을 앞에 붙여 첫 음절이 잘리지 않게 한다.
* 발화가 ``interim_interval_ms`` 이상 이어지면 중간 구간을 내보낸다
  (사양서의 Sub-Second 목표상 발화가 끝날 때까지 기다릴 수 없다).
* 무음이 ``hangover_ms`` 이상 지속되면 발화를 확정한다.
* 발화가 ``max_segment_ms``를 넘으면 강제로 끊는다 — 무한 발화에서
  STT 입력이 계속 커지는 것을 막는다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from vai_aud_vad.adapters.base import BaseVadAdapter

BYTES_PER_SAMPLE = 2


@dataclass(frozen=True)
class Segment:
    pcm: bytes
    start_ms: int
    duration_ms: int
    is_final: bool


@dataclass
class SegmenterConfig:
    hangover_ms: int = 400
    """이만큼 무음이 이어지면 발화 종료로 본다. 너무 짧으면 문장 중간에 끊기고,
    너무 길면 응답이 그만큼 늦는다."""

    speech_pad_ms: int = 200
    interim_interval_ms: int = 700
    max_segment_ms: int = 15_000
    min_segment_ms: int = 200
    """이보다 짧은 발화는 기침·클릭음으로 보고 버린다."""


@dataclass
class _Stream:
    vad: BaseVadAdapter
    frame_bytes: int
    frame_ms: float
    pending: bytearray = field(default_factory=bytearray)
    """아직 프레임 크기를 못 채운 잔여 바이트."""

    prefix: deque[bytes] = field(default_factory=deque)
    """발화 시작 전 무음 프레임 링버퍼(speech_pad용)."""

    active: bytearray = field(default_factory=bytearray)
    in_speech: bool = False
    silence_ms: float = 0.0
    speech_start_ms: float = 0.0
    emitted_ms: float = 0.0
    """이번 발화에서 마지막 interim을 내보낸 시점."""

    clock_ms: float = 0.0
    """스트림 시작 이후 누적 시간."""


class SpeechSegmenter:
    """세션/채널별 스트림을 관리하는 분할기."""

    def __init__(
        self,
        vad: BaseVadAdapter,
        sample_rate: int,
        config: SegmenterConfig | None = None,
    ) -> None:
        self._vad = vad
        self._sample_rate = sample_rate
        self.config = config or SegmenterConfig()
        self._streams: dict[str, _Stream] = {}

    def _stream(self, key: str) -> _Stream:
        stream = self._streams.get(key)
        if stream is None:
            frame_samples = self._vad.frame_samples(self._sample_rate)
            stream = _Stream(
                vad=self._vad.clone(),
                frame_bytes=frame_samples * BYTES_PER_SAMPLE,
                frame_ms=frame_samples / self._sample_rate * 1000,
            )
            pad_frames = max(1, int(self.config.speech_pad_ms / stream.frame_ms))
            stream.prefix = deque(maxlen=pad_frames)
            self._streams[key] = stream
        return stream

    def push(self, key: str, pcm: bytes) -> list[Segment]:
        """오디오 청크를 밀어 넣고 완성된 구간들을 돌려준다."""
        stream = self._stream(key)
        stream.pending.extend(pcm)
        segments: list[Segment] = []

        while len(stream.pending) >= stream.frame_bytes:
            frame = bytes(stream.pending[: stream.frame_bytes])
            del stream.pending[: stream.frame_bytes]
            segments.extend(self._consume_frame(stream, frame))

        return segments

    def is_speaking(self, key: str) -> bool:
        """지금 발화 중인가.

        끼어들기 감지가 이 값을 본다. 구간이 **확정되기를 기다리지 않고**
        말이 시작된 그 순간을 알아야 하므로, 내부 상태를 그대로 노출한다.
        """
        stream = self._streams.get(key)
        return bool(stream and stream.in_speech)

    def _consume_frame(self, stream: _Stream, frame: bytes) -> list[Segment]:
        cfg = self.config
        out: list[Segment] = []
        is_speech = stream.vad.is_speech(frame, self._sample_rate)
        stream.clock_ms += stream.frame_ms

        if is_speech:
            if not stream.in_speech:
                stream.in_speech = True
                stream.active = bytearray(b"".join(stream.prefix))
                pad_ms = len(stream.prefix) * stream.frame_ms
                stream.speech_start_ms = max(0.0, stream.clock_ms - stream.frame_ms - pad_ms)
                stream.emitted_ms = stream.speech_start_ms
                stream.prefix.clear()
            stream.active.extend(frame)
            stream.silence_ms = 0.0

            elapsed = stream.clock_ms - stream.speech_start_ms
            if elapsed >= cfg.max_segment_ms:
                out.append(self._finalize(stream))
            elif stream.clock_ms - stream.emitted_ms >= cfg.interim_interval_ms:
                stream.emitted_ms = stream.clock_ms
                out.append(
                    Segment(
                        pcm=bytes(stream.active),
                        start_ms=int(stream.speech_start_ms),
                        duration_ms=int(elapsed),
                        is_final=False,
                    )
                )
            return out

        if stream.in_speech:
            # 발화 중의 짧은 무음은 문장 내 쉼일 수 있으므로 일단 담아 둔다.
            stream.active.extend(frame)
            stream.silence_ms += stream.frame_ms
            if stream.silence_ms >= cfg.hangover_ms:
                out.append(self._finalize(stream))
        else:
            stream.prefix.append(frame)
        return out

    def _finalize(self, stream: _Stream) -> Segment:
        duration_ms = int(stream.clock_ms - stream.speech_start_ms)
        segment = Segment(
            pcm=bytes(stream.active),
            start_ms=int(stream.speech_start_ms),
            duration_ms=duration_ms,
            is_final=True,
        )
        stream.in_speech = False
        stream.active = bytearray()
        stream.silence_ms = 0.0
        stream.prefix.clear()
        return segment

    def flush(self, key: str) -> Segment | None:
        """스트림 종료 시 남은 발화를 확정한다(통화가 발화 도중 끊긴 경우)."""
        stream = self._streams.pop(key, None)
        if stream is None or not stream.in_speech:
            return None
        return self._finalize(stream)

    def drop(self, key: str) -> None:
        self._streams.pop(key, None)

    def is_meaningful(self, segment: Segment) -> bool:
        """너무 짧은 구간을 STT에 보내지 않기 위한 필터."""
        return segment.duration_ms >= self.config.min_segment_ms
