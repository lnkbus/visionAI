"""RTP 수신 — 패킷 파싱과 지터 버퍼.

전화망 오디오는 UDP로 오므로 순서가 뒤바뀌고 유실된다. 그대로 STT에 넣으면
문장이 뒤섞이고 발화 경계가 무너진다. 지터 버퍼가 하는 일:

* **재정렬** — 시퀀스 번호로 순서를 되돌린다
* **손실 보정** — 빠진 패킷 자리를 무음으로 채워 길이를 유지한다
* **지연 상한** — 무한정 기다리지 않는다. 늦게 온 패킷은 버린다

버퍼 깊이는 지연과 품질의 교환이다. 깊게 잡으면 재정렬은 잘 되지만 그만큼
응답이 늦고, 얕으면 반대다. AICC는 초저지연이 목표이므로 얕게 잡는다.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from vai_aud_rtp import codec

log = logging.getLogger(__name__)

RTP_VERSION = 2
HEADER_BYTES = 12
SEQ_MODULO = 1 << 16
"""시퀀스 번호는 16비트다. 65535 다음은 0으로 돌아간다."""

TIMESTAMP_MODULO = 1 << 32


class RtpParseError(ValueError):
    """RTP로 해석할 수 없는 패킷."""


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes

    @classmethod
    def parse(cls, data: bytes) -> RtpPacket:
        """RFC 3550 헤더를 해석한다."""
        if len(data) < HEADER_BYTES:
            raise RtpParseError(f"헤더보다 짧다: {len(data)} 바이트")

        first, second, sequence, timestamp, ssrc = struct.unpack("!BBHII", data[:HEADER_BYTES])
        version = first >> 6
        if version != RTP_VERSION:
            raise RtpParseError(f"지원하지 않는 RTP 버전: {version}")

        csrc_count = first & 0x0F
        has_padding = bool(first & 0x20)
        has_extension = bool(first & 0x10)

        offset = HEADER_BYTES + csrc_count * 4
        if has_extension:
            # 확장 헤더: 프로필(2) + 길이(2, 32bit 워드 수) + 본문
            if len(data) < offset + 4:
                raise RtpParseError("확장 헤더가 잘렸다")
            (extension_words,) = struct.unpack("!H", data[offset + 2 : offset + 4])
            offset += 4 + extension_words * 4

        if offset > len(data):
            raise RtpParseError("헤더 길이가 패킷을 넘는다")

        payload = data[offset:]
        if has_padding and payload:
            # 마지막 바이트가 패딩 길이(자기 자신 포함)를 담는다.
            pad = payload[-1]
            if pad == 0 or pad > len(payload):
                raise RtpParseError("패딩 길이가 잘못됐다")
            payload = payload[:-pad]

        return cls(
            payload_type=second & 0x7F,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            marker=bool(second & 0x80),
            payload=payload,
        )


def seq_delta(later: int, earlier: int) -> int:
    """시퀀스 차이를 랩어라운드를 고려해 계산한다.

    65535 → 0 을 -65535가 아니라 +1로 본다. 이걸 놓치면 통화 18분쯤에서
    (8kHz·20ms 기준 랩 주기) 버퍼가 통째로 리셋된다.
    """
    return ((later - earlier + SEQ_MODULO // 2) % SEQ_MODULO) - SEQ_MODULO // 2


@dataclass
class JitterBufferConfig:
    depth: int = 3
    """버퍼에 쌓아 두는 패킷 수. 20ms 패킷 기준 3개면 60ms 지연."""

    max_gap: int = 25
    """이보다 큰 시퀀스 공백은 메우지 않고 재동기화한다.
    (20ms 기준 0.5초 — 이 이상 잃었으면 통화가 끊겼다고 보는 편이 맞다)"""

    samples_per_packet: int = 160
    """8kHz·20ms 기준. 손실 구간 무음 길이를 정하는 데 쓴다."""


@dataclass
class JitterBufferStats:
    received: int = 0
    reordered: int = 0
    lost: int = 0
    late: int = 0
    duplicated: int = 0
    resynced: int = 0


@dataclass
class JitterBuffer:
    """스트림 하나(= SSRC 하나)의 재정렬 버퍼."""

    config: JitterBufferConfig = field(default_factory=JitterBufferConfig)
    stats: JitterBufferStats = field(default_factory=JitterBufferStats)
    _pending: dict[int, RtpPacket] = field(default_factory=dict)
    _next_seq: int | None = None

    def push(self, packet: RtpPacket) -> list[bytes]:
        """패킷을 넣고, 순서가 확정된 PCM 조각들을 돌려준다."""
        self.stats.received += 1

        if self._next_seq is None:
            self._next_seq = packet.sequence

        gap = seq_delta(packet.sequence, self._next_seq)
        if gap < 0:
            # 이미 지나간 자리. 늦게 도착했거나 중복이다.
            self.stats.late += 1
            return []
        if packet.sequence in self._pending:
            self.stats.duplicated += 1
            return []
        if gap > self.config.max_gap:
            # 너무 멀리 앞선 패킷 — 통화가 끊겼다 이어졌거나 SSRC가 바뀐 경우.
            # 옛 자리를 기다리며 버퍼를 붙들고 있으면 그만큼 오디오가 멈춘다.
            log.info("시퀀스 재동기화", extra={"gap": gap, "sequence": packet.sequence})
            self.stats.resynced += 1
            self._pending.clear()
            self._next_seq = packet.sequence

        if gap > 0:
            self.stats.reordered += 1
        self._pending[packet.sequence] = packet

        return self._drain()

    def _drain(self) -> list[bytes]:
        """버퍼 깊이를 넘긴 만큼 순서대로 내보낸다."""
        out: list[bytes] = []
        while len(self._pending) > self.config.depth:
            assert self._next_seq is not None
            packet = self._pending.pop(self._next_seq, None)
            if packet is None:
                # 이 자리는 결국 안 왔다. 무음으로 길이를 맞춘다 —
                # 건너뛰면 오디오가 짧아져 발화 경계가 어긋난다.
                self.stats.lost += 1
                out.append(codec.silence(self.config.samples_per_packet))
            else:
                out.append(codec.decode(packet.payload, packet.payload_type))
            self._next_seq = (self._next_seq + 1) % SEQ_MODULO
        return [chunk for chunk in out if chunk]

    def flush(self) -> list[bytes]:
        """통화 종료 시 버퍼에 남은 것을 모두 내보낸다."""
        out: list[bytes] = []
        for sequence in sorted(self._pending, key=lambda s: seq_delta(s, self._next_seq or 0)):
            packet = self._pending[sequence]
            chunk = codec.decode(packet.payload, packet.payload_type)
            if chunk:
                out.append(chunk)
        self._pending.clear()
        self._next_seq = None
        return out
