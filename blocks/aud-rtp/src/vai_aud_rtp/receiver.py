"""RTP UDP 수신기 — 미디어를 파이프라인에 넣는다.

포트당 하나의 asyncio 데이터그램 엔드포인트를 연다. 수신 콜백은 동기이므로
버스 발행은 태스크로 넘긴다 — 콜백에서 await 하면 이벤트 루프가 다음 패킷을
못 읽어 지터가 커진다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from vai_aud_rtp import codec
from vai_aud_rtp.calls import CallLeg, CallRegistry
from vai_aud_rtp.rtp import RtpPacket, RtpParseError
from vai_common.bus import EventBus
from vai_contracts.events import AudioChunk
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)

MAX_INFLIGHT = 64
"""레그당 동시 발행 태스크 상한. 버스가 밀릴 때 태스크가 무한정 쌓이는 것을 막는다."""

PublishFn = Callable[[CallLeg, bytes], Coroutine[Any, Any, None]]


class RtpProtocol(asyncio.DatagramProtocol):
    """포트 하나를 담당하는 수신 프로토콜."""

    def __init__(self, leg: CallLeg, publish: PublishFn) -> None:
        self._leg = leg
        self._publish = publish
        self._tasks: set[asyncio.Task[None]] = set()
        self._warned_types: set[int] = set()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            packet = RtpPacket.parse(data)
        except RtpParseError:
            # 포트 스캔이나 STUN 같은 비-RTP 트래픽. 조용히 버린다.
            return

        if not codec.is_supported(packet.payload_type):
            if packet.payload_type not in self._warned_types:
                # 같은 통화에서 매 패킷 경고하면 로그가 마비된다. 한 번만 남긴다.
                self._warned_types.add(packet.payload_type)
                log.warning(
                    "지원하지 않는 페이로드 타입",
                    extra={
                        "session_id": self._leg.session_id,
                        "payload_type": packet.payload_type,
                    },
                )
            return

        if self._leg.ssrc is None:
            self._leg.ssrc = packet.ssrc
        elif packet.ssrc != self._leg.ssrc:
            # 다른 통화가 같은 포트로 들어왔다. 섞으면 두 통화가 한 자막이 된다.
            log.warning(
                "예상과 다른 SSRC — 패킷 폐기",
                extra={"session_id": self._leg.session_id, "port": self._leg.port},
            )
            return

        for chunk in self._leg.buffer.push(packet):
            self._emit(chunk)

    def _emit(self, pcm: bytes) -> None:
        if len(self._tasks) >= MAX_INFLIGHT:
            # 버스가 밀리는 상황. 오래된 오디오를 붙들고 있어 봐야 쓸모없다.
            log.warning(
                "발행 큐 포화 — 청크 폐기",
                extra={"session_id": self._leg.session_id, "port": self._leg.port},
            )
            return
        task = asyncio.create_task(self._publish(self._leg, pcm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def flush(self) -> None:
        """통화 종료 시 버퍼 잔여분을 내보낸다."""
        for chunk in self._leg.buffer.flush():
            self._emit(chunk)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class RtpReceiver:
    """통화 레그별 UDP 엔드포인트를 여닫는다."""

    def __init__(self, bus: EventBus, registry: CallRegistry, *, host: str = "0.0.0.0") -> None:
        self._bus = bus
        self._registry = registry
        self._host = host
        self._endpoints: dict[int, tuple[asyncio.DatagramTransport, RtpProtocol]] = {}

    async def open_leg(self, leg: CallLeg) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: RtpProtocol(leg, self._publish), local_addr=(self._host, leg.port)
        )
        self._endpoints[leg.port] = (transport, protocol)
        log.info("RTP 수신 시작", extra={"session_id": leg.session_id, "port": leg.port})

    async def close_leg(self, leg: CallLeg) -> None:
        endpoint = self._endpoints.pop(leg.port, None)
        if endpoint is None:
            return
        transport, protocol = endpoint
        protocol.flush()
        await protocol.drain()
        transport.close()
        log.info(
            "RTP 수신 종료",
            extra={
                "session_id": leg.session_id,
                "port": leg.port,
                "received": leg.buffer.stats.received,
                "lost": leg.buffer.stats.lost,
                "late": leg.buffer.stats.late,
            },
        )

    async def close_all(self) -> None:
        for port in list(self._endpoints):
            leg = self._registry.leg_for_port(port)
            if leg is not None:
                await self.close_leg(leg)
            else:  # pragma: no cover - 레지스트리가 먼저 정리된 경우
                transport, _ = self._endpoints.pop(port)
                transport.close()

    async def _publish(self, leg: CallLeg, pcm: bytes) -> None:
        try:
            await self._bus.publish(
                Topic.AUDIO_IN,
                AudioChunk(
                    session_id=leg.session_id,
                    tenant_id=leg.tenant_id,
                    seq=leg.next_seq(),
                    channel=leg.channel,
                    sample_rate=leg.sample_rate,
                    pcm=pcm,
                    profile=leg.profile,
                ),
            )
        except Exception:
            # 태스크로 떼어 낸 발행이라 여기서 삼키면 아무도 모른다.
            # 통화는 계속 받되(끊으면 더 나쁘다) 실패는 반드시 남긴다.
            log.exception(
                "오디오 청크 발행 실패",
                extra={"session_id": leg.session_id, "port": leg.port},
            )
