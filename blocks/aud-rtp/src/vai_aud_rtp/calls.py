"""통화 레그 관리 — RTP 포트와 세션의 연결.

**SIP 시그널링을 이 블록에 넣지 않은 이유**: 고객사마다 통화 미디어를 받는
방식이 다르다. SIPREC으로 복제를 받는 곳, 미디어 게이트웨이가 포트로 밀어
주는 곳, CTI 이벤트로 통제하는 곳이 있다. 어느 하나를 가정해 SIP UA를 구현하면
대부분의 고객사에서 쓸 수 없는 코드가 된다.

대신 **제어 인터페이스를 열어 둔다**: 고객사의 CTI/PBX 연동이 통화 시작/종료를
알려 주면, 이 블록은 포트를 할당하고 미디어를 받아 파이프라인에 넣는다.
SIP UA가 필요한 고객사는 이 제어 API를 호출하는 어댑터를 추가하면 된다.

포트로 채널을 가른다. 고객·상담원 각각 다른 포트로 받는 것이 SIPREC과
미디어 게이트웨이 양쪽에서 가장 흔한 형태이고, G.711은 애초에 모노라
한 스트림에 두 채널을 담을 수 없다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from vai_aud_rtp.rtp import JitterBuffer, JitterBufferConfig
from vai_contracts.session import ChannelRole, SessionProfile

log = logging.getLogger(__name__)


class PortPoolExhausted(RuntimeError):
    """할당 가능한 RTP 포트가 없다."""


@dataclass
class CallLeg:
    """통화 한 건의 한 채널(고객 또는 상담원)."""

    session_id: str
    tenant_id: str
    profile: SessionProfile
    channel: ChannelRole
    port: int
    sample_rate: int = 8000
    """G.711은 8kHz다. 리샘플링은 STT 어댑터가 맡는다 — 여기서 올리면
    파이프라인 전 구간의 대역폭이 두 배가 된다."""

    buffer: JitterBuffer = field(default_factory=JitterBuffer)
    seq: int = 0
    ssrc: int | None = None
    """첫 패킷의 SSRC를 기억한다. 바뀌면 다른 통화가 같은 포트로 들어온 것이다."""

    last_packet_at: float = field(default_factory=time.monotonic)
    """마지막 RTP 수신 시각(단조 시계).

    통화 개설 시각으로 시작한다 — 미디어가 한 번도 안 온 레그도 같은 기준으로
    회수되게 하기 위해서다. 벽시계가 아니라 단조 시계를 쓴다: NTP 보정으로
    시간이 뒤로 가면 멀쩡한 통화가 끊긴다."""

    def touch(self) -> None:
        self.last_packet_at = time.monotonic()

    def idle_for(self) -> float:
        """마지막 패킷 이후 흐른 초."""
        return time.monotonic() - self.last_packet_at

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class PortPool:
    """RTP 포트 할당기.

    RTP는 관례상 짝수 포트를 쓰고 홀수는 RTCP가 가져간다. 그 관례를 지켜야
    상대 미디어 게이트웨이가 RTCP를 제대로 보낸다.
    """

    def __init__(self, start: int = 40000, end: int = 40100) -> None:
        if start % 2:
            start += 1
        self._free = list(range(start, end, 2))
        self._used: set[int] = set()

    @property
    def available(self) -> int:
        return len(self._free)

    def acquire(self) -> int:
        if not self._free:
            raise PortPoolExhausted("RTP 포트 풀이 소진되었다")
        port = self._free.pop(0)
        self._used.add(port)
        return port

    def release(self, port: int) -> None:
        if port in self._used:
            self._used.discard(port)
            # 뒤에 넣는다. 방금 끊긴 통화의 잔여 패킷이 새 통화로 섞이는 것을
            # 최대한 늦춘다.
            self._free.append(port)


class CallRegistry:
    """포트 → 통화 레그 조회."""

    def __init__(self, pool: PortPool) -> None:
        self._pool = pool
        self._by_port: dict[int, CallLeg] = {}
        self._by_session: dict[str, list[CallLeg]] = {}

    @property
    def active_calls(self) -> int:
        return len(self._by_session)

    @property
    def active_legs(self) -> int:
        return len(self._by_port)

    @property
    def free_ports(self) -> int:
        """남은 수용량. 라이선스 동시 채널 수와 함께 감시한다."""
        return self._pool.available

    def open(
        self,
        session_id: str,
        tenant_id: str,
        profile: SessionProfile,
        channels: list[ChannelRole],
        *,
        buffer_config: JitterBufferConfig | None = None,
    ) -> list[CallLeg]:
        """통화 하나에 대해 채널별 레그를 만든다."""
        legs: list[CallLeg] = []
        try:
            for channel in channels:
                port = self._pool.acquire()
                leg = CallLeg(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    profile=profile,
                    channel=channel,
                    port=port,
                    buffer=JitterBuffer(config=buffer_config or JitterBufferConfig()),
                )
                self._by_port[port] = leg
                legs.append(leg)
        except PortPoolExhausted:
            # 일부만 할당된 채로 두면 포트가 샌다.
            for leg in legs:
                self._by_port.pop(leg.port, None)
                self._pool.release(leg.port)
            raise

        self._by_session[session_id] = legs
        log.info(
            "통화 레그 개설",
            extra={
                "session_id": session_id,
                "ports": [leg.port for leg in legs],
                "channels": [leg.channel.value for leg in legs],
            },
        )
        return legs

    def idle_sessions(self, timeout_s: float) -> list[str]:
        """모든 레그가 ``timeout_s`` 이상 조용한 세션들.

        레그 하나만 조용한 것으로는 끊지 않는다 — 한쪽이 말을 안 하는 통화는
        정상이다. 양쪽 미디어가 모두 멈춘 것이 "통화가 끝났는데 아무도
        알려 주지 않은" 상태다.
        """
        return [
            session_id
            for session_id, legs in self._by_session.items()
            if legs and all(leg.idle_for() >= timeout_s for leg in legs)
        ]

    def leg_for_port(self, port: int) -> CallLeg | None:
        return self._by_port.get(port)

    def legs_for_session(self, session_id: str) -> list[CallLeg]:
        return list(self._by_session.get(session_id, []))

    def close(self, session_id: str) -> list[CallLeg]:
        """레그를 정리하고 포트를 돌려준다."""
        legs = self._by_session.pop(session_id, [])
        for leg in legs:
            self._by_port.pop(leg.port, None)
            self._pool.release(leg.port)
        if legs:
            log.info(
                "통화 레그 종료",
                extra={"session_id": session_id, "ports": [leg.port for leg in legs]},
            )
        return legs
