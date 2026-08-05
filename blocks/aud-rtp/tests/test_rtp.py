"""RTP 수신 정확성 테스트.

전화망 오디오는 순서가 뒤바뀌고 유실된다. 그 현실을 그대로 재현해,
지터 버퍼가 자막을 뒤섞지 않는지 확인한다.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from vai_aud_rtp import codec
from vai_aud_rtp.calls import CallRegistry, PortPool, PortPoolExhausted
from vai_aud_rtp.codec import PayloadType
from vai_aud_rtp.rtp import (
    SEQ_MODULO,
    JitterBuffer,
    JitterBufferConfig,
    RtpPacket,
    RtpParseError,
    seq_delta,
)
from vai_contracts.session import ChannelRole, SessionProfile

SAMPLES = 160  # 8kHz · 20ms


def rtp(
    sequence: int,
    payload: bytes = b"\xff" * SAMPLES,
    *,
    payload_type: int = PayloadType.PCMU,
    ssrc: int = 0x11223344,
    timestamp: int = 0,
    marker: bool = False,
    padding: int = 0,
    csrc: int = 0,
) -> bytes:
    first = (2 << 6) | (0x20 if padding else 0) | csrc
    second = (0x80 if marker else 0) | payload_type
    header = struct.pack("!BBHII", first, second, sequence % SEQ_MODULO, timestamp, ssrc)
    body = payload + (b"\x00" * (padding - 1) + bytes([padding]) if padding else b"")
    return header + b"\x00" * (csrc * 4) + body


# ── 패킷 파싱 ────────────────────────────────────────────────────────────────


def test_basic_header_is_parsed() -> None:
    packet = RtpPacket.parse(rtp(42, timestamp=1234, marker=True))

    assert packet.sequence == 42
    assert packet.timestamp == 1234
    assert packet.payload_type == PayloadType.PCMU
    assert packet.marker
    assert len(packet.payload) == SAMPLES


def test_padding_is_stripped() -> None:
    """패딩을 오디오로 취급하면 통화 끝마다 잡음이 붙는다."""
    packet = RtpPacket.parse(rtp(1, payload=b"\xff" * 8, padding=4))

    assert len(packet.payload) == 8


def test_csrc_list_is_skipped() -> None:
    """믹서를 거친 미디어에는 CSRC가 붙는다. 건너뛰지 않으면 앞부분이 잡음이 된다."""
    packet = RtpPacket.parse(rtp(1, payload=b"\xab" * 16, csrc=2))

    assert packet.payload == b"\xab" * 16


def test_extension_header_is_skipped() -> None:
    header = struct.pack("!BBHII", (2 << 6) | 0x10, PayloadType.PCMU, 1, 0, 0x99)
    extension = struct.pack("!HH", 0xBEDE, 1) + b"\x01\x02\x03\x04"

    packet = RtpPacket.parse(header + extension + b"\xcd" * 8)

    assert packet.payload == b"\xcd" * 8


@pytest.mark.parametrize(
    "data",
    [b"", b"\x80\x00", struct.pack("!BBHII", 1 << 6, 0, 1, 0, 0)],  # 짧음, 잘림, 버전 1
)
def test_malformed_packets_are_rejected(data: bytes) -> None:
    """포트 스캔·STUN 같은 비-RTP 트래픽이 파이프라인에 들어가면 안 된다."""
    with pytest.raises(RtpParseError):
        RtpPacket.parse(data)


# ── 코덱 ─────────────────────────────────────────────────────────────────────


def test_ulaw_silence_decodes_near_zero() -> None:
    """μ-law 0xFF는 무음이다. 여기가 틀리면 전 통화에 DC 오프셋이 실린다."""
    pcm = np.frombuffer(codec.decode(b"\xff" * 8, PayloadType.PCMU), dtype="<i2")

    assert np.all(np.abs(pcm) < 16)


def test_ulaw_full_scale_decodes_loud() -> None:
    pcm = np.frombuffer(codec.decode(b"\x00" * 4, PayloadType.PCMU), dtype="<i2")

    assert np.all(np.abs(pcm) > 30000)


def test_alaw_silence_decodes_near_zero() -> None:
    pcm = np.frombuffer(codec.decode(b"\xd5" * 8, PayloadType.PCMA), dtype="<i2")

    assert np.all(np.abs(pcm) < 16)


def test_decoded_length_doubles() -> None:
    """8bit 압축 → 16bit 선형. 길이가 안 맞으면 프레임이 통째로 어긋난다."""
    assert len(codec.decode(b"\x55" * SAMPLES, PayloadType.PCMU)) == SAMPLES * 2


def test_l16_big_endian_is_converted() -> None:
    """RFC 3551의 L16은 빅엔디안이다. 그대로 쓰면 잡음이 된다."""
    payload = np.array([1000, -1000], dtype=">i2").tobytes()

    pcm = np.frombuffer(codec.decode(payload, PayloadType.L16), dtype="<i2")

    assert list(pcm) == [1000, -1000]


def test_unknown_payload_type_yields_nothing() -> None:
    """잡음을 흘려보내느니 그 구간을 버린다."""
    assert codec.decode(b"\x01\x02", 96) == b""
    assert not codec.is_supported(96)


# ── 시퀀스 랩어라운드 ────────────────────────────────────────────────────────


def test_sequence_wraparound_is_handled() -> None:
    """65535 → 0을 -65535로 보면 통화 중간에 버퍼가 통째로 리셋된다."""
    assert seq_delta(0, 65535) == 1
    assert seq_delta(65535, 0) == -1
    assert seq_delta(10, 5) == 5


# ── 지터 버퍼 ────────────────────────────────────────────────────────────────


def buffer(depth: int = 2, max_gap: int = 25) -> JitterBuffer:
    return JitterBuffer(
        config=JitterBufferConfig(depth=depth, max_gap=max_gap, samples_per_packet=SAMPLES)
    )


def push(jb: JitterBuffer, *sequences: int, payload: bytes | None = None) -> list[bytes]:
    out: list[bytes] = []
    for sequence in sequences:
        body = payload or bytes([sequence % 256]) * SAMPLES
        out.extend(jb.push(RtpPacket.parse(rtp(sequence, body))))
    return out


def test_in_order_packets_pass_through() -> None:
    jb = buffer(depth=1)

    chunks = push(jb, 1, 2, 3, 4)

    assert len(chunks) == 3  # depth=1 만큼 버퍼에 남는다
    assert jb.stats.lost == 0


def marker_of(chunk: bytes) -> int:
    """청크를 만든 시퀀스 번호를 되찾는다.

    각 패킷을 ``bytes([seq]) * N``으로 채웠으므로, 디코드된 첫 샘플이 그
    바이트의 μ-law 값과 같다. 역인덱싱하면 원래 시퀀스가 나온다.
    """
    first = int(np.frombuffer(chunk, dtype="<i2")[0])
    return int(np.where(codec.ULAW_TABLE == first)[0][0])


def test_reordered_packets_come_out_in_order() -> None:
    """순서가 뒤바뀐 채로 STT에 들어가면 문장이 뒤섞인다."""
    jb = buffer(depth=2)
    arrival = [1, 3, 2, 5, 4, 6, 7]

    emitted: list[bytes] = []
    for seq in arrival:
        emitted.extend(jb.push(RtpPacket.parse(rtp(seq, bytes([seq]) * SAMPLES))))
    emitted.extend(jb.flush())

    assert jb.stats.reordered > 0, "재정렬이 실제로 일어난 시나리오여야 한다"
    assert [marker_of(chunk) for chunk in emitted] == sorted(arrival)


def test_ordering_survives_a_loss_in_the_middle() -> None:
    """손실 자리는 무음으로 메우되, 앞뒤 순서는 그대로여야 한다."""
    jb = buffer(depth=1)
    emitted: list[bytes] = []
    for seq in (1, 2, 4, 5):  # 3번 유실
        emitted.extend(jb.push(RtpPacket.parse(rtp(seq, bytes([seq]) * SAMPLES))))
    emitted.extend(jb.flush())

    markers = [
        "silence" if chunk == codec.silence(SAMPLES) else marker_of(chunk) for chunk in emitted
    ]

    assert markers == [1, 2, "silence", 4, 5]


def test_lost_packet_is_filled_with_silence() -> None:
    """건너뛰면 오디오가 짧아져 발화 경계가 어긋난다."""
    jb = buffer(depth=1)

    chunks = push(jb, 1, 2, 4, 5, 6)

    assert jb.stats.lost == 1
    assert any(chunk == codec.silence(SAMPLES) for chunk in chunks)


def test_late_packet_is_dropped() -> None:
    """이미 지나간 자리에 끼워 넣으면 오디오가 과거로 되돌아간다."""
    jb = buffer(depth=1)
    push(jb, 10, 11, 12, 13)

    push(jb, 5)

    assert jb.stats.late == 1


def test_duplicate_packet_is_ignored() -> None:
    jb = buffer(depth=3)
    push(jb, 1, 2)

    push(jb, 2)

    assert jb.stats.duplicated == 1


def test_large_gap_triggers_resync() -> None:
    """통화가 끊겼다 이어진 경우. 옛 자리를 기다리면 그만큼 오디오가 멈춘다."""
    jb = buffer(depth=1, max_gap=10)
    push(jb, 1, 2)

    push(jb, 500, 501, 502)

    assert jb.stats.resynced == 1
    assert jb.stats.lost == 0, "재동기화는 손실로 세지 않는다"


def test_wraparound_does_not_resync() -> None:
    jb = buffer(depth=1)
    push(jb, 65533, 65534, 65535, 0, 1, 2)

    assert jb.stats.resynced == 0
    assert jb.stats.lost == 0


# ── 포트 풀 · 레지스트리 ─────────────────────────────────────────────────────


def test_pool_allocates_even_ports_only() -> None:
    """홀수 포트는 RTCP 몫이다. 관례를 어기면 상대가 RTCP를 못 보낸다."""
    pool = PortPool(40000, 40010)

    ports = [pool.acquire() for _ in range(pool.available)]

    assert all(port % 2 == 0 for port in ports)


def test_pool_exhaustion_is_explicit() -> None:
    pool = PortPool(40000, 40004)
    for _ in range(pool.available):
        pool.acquire()

    with pytest.raises(PortPoolExhausted):
        pool.acquire()


def test_released_ports_return_to_the_pool() -> None:
    pool = PortPool(40000, 40004)
    port = pool.acquire()
    before = pool.available

    pool.release(port)

    assert pool.available == before + 1


def test_partial_allocation_does_not_leak_ports() -> None:
    """일부만 할당된 채 실패하면 포트가 샌다."""
    registry = CallRegistry(PortPool(40000, 40004))  # 포트 2개
    before = registry._pool.available

    with pytest.raises(PortPoolExhausted):
        registry.open(
            "s1",
            "t1",
            SessionProfile.AICC,
            [ChannelRole.CUSTOMER, ChannelRole.AGENT, ChannelRole.PARTICIPANT],
        )

    assert registry._pool.available == before
    assert registry.active_legs == 0


def test_channels_get_separate_ports() -> None:
    """한 포트에 두 채널을 담으면 고객과 상담원 발화가 한 줄로 섞인다."""
    registry = CallRegistry(PortPool(40000, 40100))

    legs = registry.open("s1", "t1", SessionProfile.AICC, [ChannelRole.CUSTOMER, ChannelRole.AGENT])

    assert len({leg.port for leg in legs}) == 2
    assert {leg.channel for leg in legs} == {ChannelRole.CUSTOMER, ChannelRole.AGENT}
    assert registry.leg_for_port(legs[0].port) is legs[0]


def test_closing_a_call_frees_its_ports() -> None:
    pool = PortPool(40000, 40100)
    registry = CallRegistry(pool)
    registry.open("s1", "t1", SessionProfile.AICC, [ChannelRole.CUSTOMER, ChannelRole.AGENT])
    before = pool.available

    registry.close("s1")

    assert pool.available == before + 2
    assert registry.active_calls == 0
    assert registry.legs_for_session("s1") == []


def test_call_legs_carry_telephony_sample_rate() -> None:
    """G.711은 8kHz다. 여기서 16k로 적으면 STT가 두 배 빠른 음성을 듣는다."""
    registry = CallRegistry(PortPool(40000, 40100))

    legs = registry.open("s1", "t1", SessionProfile.AICC, [ChannelRole.CUSTOMER])

    assert legs[0].sample_rate == 8000
