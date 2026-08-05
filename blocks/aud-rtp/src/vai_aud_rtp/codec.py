"""G.711 디코더 — 전화망 오디오를 PCM16으로.

파이썬 표준 ``audioop``은 3.13에서 제거되므로 직접 구현한다. ITU-T G.711
참조 알고리즘으로 256엔트리 룩업 테이블을 한 번 만들고, 이후에는 numpy
인덱싱으로 바이트 배열을 통째로 변환한다 — 샘플당 파이썬 루프를 돌면
100채널 규모에서 CPU가 버티지 못한다.

전화망은 8kHz μ-law(한국·북미) 또는 A-law(유럽)를 쓴다. 어느 쪽이 오는지는
SDP 협상 결과이므로 페이로드 타입으로 판별한다.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

# G.711 참조 구현 상수
_SIGN_BIT = 0x80
_QUANT_MASK = 0x0F
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_BIAS = 0x84


class PayloadType(IntEnum):
    """RFC 3551 정적 페이로드 타입."""

    PCMU = 0
    """G.711 μ-law, 8kHz mono."""

    PCMA = 8
    """G.711 A-law, 8kHz mono."""

    L16 = 11
    """16bit 선형 PCM. 일부 미디어 서버가 변환해서 보낸다."""


def _ulaw_sample(value: int) -> int:
    """μ-law 1바이트 → 16bit 선형."""
    value = ~value & 0xFF
    magnitude = ((value & _QUANT_MASK) << 3) + _BIAS
    magnitude <<= (value & _SEG_MASK) >> _SEG_SHIFT
    return (_BIAS - magnitude) if (value & _SIGN_BIT) else (magnitude - _BIAS)


def _alaw_sample(value: int) -> int:
    """A-law 1바이트 → 16bit 선형."""
    value ^= 0x55
    magnitude = (value & _QUANT_MASK) << 4
    segment = (value & _SEG_MASK) >> _SEG_SHIFT
    if segment == 0:
        magnitude += 8
    elif segment == 1:
        magnitude += 0x108
    else:
        magnitude = (magnitude + 0x108) << (segment - 1)
    return magnitude if (value & _SIGN_BIT) else -magnitude


def _build_table(decode: object) -> np.ndarray:
    return np.array([decode(byte) for byte in range(256)], dtype=np.int16)  # type: ignore[operator]


ULAW_TABLE = _build_table(_ulaw_sample)
ALAW_TABLE = _build_table(_alaw_sample)


def decode(payload: bytes, payload_type: int) -> bytes:
    """RTP 페이로드를 PCM16 리틀엔디안으로 변환한다.

    알 수 없는 페이로드 타입은 빈 바이트를 돌려준다 — 잡음을 파이프라인에
    흘려보내느니 그 구간을 버리는 편이 낫다. 호출부가 로그를 남긴다.
    """
    if not payload:
        return b""
    if payload_type == PayloadType.PCMU:
        return ULAW_TABLE[np.frombuffer(payload, dtype=np.uint8)].tobytes()
    if payload_type == PayloadType.PCMA:
        return ALAW_TABLE[np.frombuffer(payload, dtype=np.uint8)].tobytes()
    if payload_type == PayloadType.L16:
        # RFC 3551의 L16은 빅엔디안이다. 파이프라인은 리틀엔디안을 쓴다.
        if len(payload) % 2:
            return b""
        return np.frombuffer(payload, dtype=">i2").astype("<i2").tobytes()
    return b""


def is_supported(payload_type: int) -> bool:
    return payload_type in {PayloadType.PCMU, PayloadType.PCMA, PayloadType.L16}


def silence(samples: int) -> bytes:
    """손실 구간을 메울 무음.

    G.711에는 손실 은닉(PLC)이 없다. 잃은 패킷을 그냥 건너뛰면 오디오가
    짧아져 타임스탬프와 어긋나고, VAD가 발화 경계를 잘못 잡는다. 무음으로
    길이를 맞추는 것이 최소한의 처리다.
    """
    return b"\x00\x00" * max(0, samples)
