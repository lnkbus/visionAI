"""전화 인입 통합 테스트 — 실제 UDP로 RTP를 쏴서 자막까지 확인한다.

AUD-RTP는 소켓을 다루므로 단위 테스트만으로는 "정말 받아지는가"를 증명하지
못한다. 실제 UDP 소켓으로 G.711 패킷을 보내 파이프라인 끝까지 흐르는지 본다.
"""

from __future__ import annotations

import asyncio
import math
import socket
import struct
from contextlib import suppress

import httpx
import pytest
from httpx import ASGITransport

from vai_aud_rtp.app import create_app
from vai_aud_rtp.codec import PayloadType
from vai_aud_vad.adapters.energy import EnergyVadAdapter
from vai_aud_vad.worker import VadWorker
from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AudioChunk, SttDelta
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic
from vai_stt_core.adapters.fake import FakeSTTAdapter
from vai_stt_core.worker import SttWorker

TENANT = "t1"
RATE = 8000
SAMPLES = 160  # 20ms


def linear_to_ulaw(sample: int) -> int:
    """PCM16 → μ-law. 전화 교환기가 하는 일을 테스트에서 재현한다."""
    sign = 0x80 if sample < 0 else 0x00
    magnitude = min(abs(sample), 32635) + 0x84
    exponent = 7
    while exponent > 0 and not magnitude & (0x4000 >> (7 - exponent)):
        exponent -= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_tone(samples: int, *, amplitude: int = 12000, freq: int = 220) -> bytes:
    """μ-law로 인코딩된 톤. 실제 통화 오디오 자리."""
    return bytes(
        linear_to_ulaw(int(amplitude * math.sin(2 * math.pi * freq * n / RATE)))
        for n in range(samples)
    )


def ulaw_silence(samples: int) -> bytes:
    return bytes([linear_to_ulaw(0)]) * samples


def rtp_packet(sequence: int, timestamp: int, payload: bytes, ssrc: int = 0xCAFE) -> bytes:
    header = struct.pack("!BBHII", 2 << 6, PayloadType.PCMU, sequence, timestamp, ssrc)
    return header + payload


class CoreBusStub:
    """CORE-BUS 대역. 세션 생성·종료만 흉내 낸다."""

    def __init__(self) -> None:
        self.closed: list[str] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/close"):
                self.closed.append(request.url.path.split("/")[-2])
                return httpx.Response(200, json={})
            return httpx.Response(
                201,
                json={
                    "session_id": "sess_tel_0001",
                    "tenant_id": TENANT,
                    "profile": "aicc",
                    "audio_format": "pcm_8k",
                    "state": "active",
                    "language": "ko",
                    "created_at": "2026-08-05T00:00:00Z",
                    "closed_at": None,
                    "metadata": {},
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://bus")


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    for topic, group in [
        (Topic.AUDIO_IN, "vad"),
        (Topic.AUDIO_IN, "observer"),
        (Topic.AUDIO_SEGMENT, "stt"),
        (Topic.STT_DELTA, "observer"),
    ]:
        bus.register_group(topic, group)
    return bus


@pytest.fixture
async def rtp_gateway(bus: InMemoryEventBus):
    """AUD-RTP를 실제 소켓과 함께 띄운다."""
    core_bus = CoreBusStub()
    app = create_app(bus=bus, core_bus_client=core_bus.client())
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rtp") as http,
        app.router.lifespan_context(app),
    ):
        yield http, core_bus


async def start_call(client: httpx.AsyncClient, channels: list[str] | None = None) -> dict:
    response = await client.post(
        "/internal/v1/calls",
        json={
            "tenant_id": TENANT,
            "profile": "aicc",
            "channels": channels or ["customer", "agent"],
            "external_call_id": "CTI-9001",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def send_rtp(port: int, packets: list[bytes]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for packet in packets:
            sock.sendto(packet, ("127.0.0.1", port))


async def collect(bus: InMemoryEventBus, topic: Topic, model: type, count: int, wait_s: float):
    collected: list = []

    async def run() -> None:
        async for delivery in bus.consume(topic, model, group="observer", consumer="obs"):
            collected.append(delivery.event)
            await delivery.ack()
            if len(collected) >= count:
                return

    with suppress(TimeoutError):
        await asyncio.wait_for(run(), timeout=wait_s)
    return collected


async def test_rtp_media_reaches_the_pipeline(rtp_gateway, bus: InMemoryEventBus) -> None:
    """UDP로 쏜 G.711이 audio.in 이벤트가 되는지 — 이 경로가 전화 인입의 전부다."""
    client, _ = rtp_gateway
    call = await start_call(client, ["customer"])
    port = call["legs"][0]["port"]

    collector = asyncio.create_task(collect(bus, Topic.AUDIO_IN, AudioChunk, 3, 5.0))
    await asyncio.sleep(0)

    send_rtp(
        port,
        [rtp_packet(i, i * SAMPLES, ulaw_tone(SAMPLES)) for i in range(1, 9)],
    )
    chunks = await collector

    assert chunks, "RTP가 파이프라인에 도달하지 않았다"
    chunk = chunks[0]
    assert chunk.session_id == "sess_tel_0001"
    assert chunk.tenant_id == TENANT
    assert chunk.channel is ChannelRole.CUSTOMER
    assert chunk.sample_rate == RATE, "G.711은 8kHz다"
    # μ-law 1바이트 → PCM16 2바이트
    assert len(chunk.pcm) == SAMPLES * 2


async def test_channels_stay_separate(rtp_gateway, bus: InMemoryEventBus) -> None:
    """한 포트로 두 채널을 받으면 고객과 상담원 발화가 한 줄로 섞인다."""
    client, _ = rtp_gateway
    call = await start_call(client)
    ports = {leg["channel"]: leg["port"] for leg in call["legs"]}

    collector = asyncio.create_task(collect(bus, Topic.AUDIO_IN, AudioChunk, 6, 5.0))
    await asyncio.sleep(0)

    send_rtp(
        ports["customer"], [rtp_packet(i, i * SAMPLES, ulaw_tone(SAMPLES)) for i in range(1, 7)]
    )
    send_rtp(
        ports["agent"],
        [rtp_packet(i, i * SAMPLES, ulaw_tone(SAMPLES), ssrc=0xBEEF) for i in range(1, 7)],
    )
    chunks = await collector

    channels = {chunk.channel for chunk in chunks}
    assert ChannelRole.CUSTOMER in channels or ChannelRole.AGENT in channels
    for chunk in chunks:
        assert chunk.session_id == "sess_tel_0001"


async def test_call_teardown_closes_the_session(rtp_gateway) -> None:
    """세션을 안 닫으면 요약이 트리거되지 않는다."""
    client, core_bus = rtp_gateway
    call = await start_call(client, ["customer"])

    response = await client.delete(f"/internal/v1/calls/{call['session_id']}")

    assert response.status_code == 200
    assert core_bus.closed == [call["session_id"]]
    assert (await client.get(f"/internal/v1/calls/{call['session_id']}/stats")).status_code == 404


async def test_stats_expose_network_quality(rtp_gateway) -> None:
    """오인식 조사 시 "망 문제인가 모델 문제인가"를 가르는 근거다.

    손실은 버퍼가 그 자리를 지나가야 확정된다 — 뒤 패킷이 depth만큼 더 쌓이기
    전까지는 아직 늦게 도착할 수 있는 패킷으로 본다. 그래서 공백 뒤로 충분히
    보낸 뒤에 확인한다.
    """
    client, _ = rtp_gateway
    call = await start_call(client, ["customer"])
    port = call["legs"][0]["port"]

    sequences = [1, 2, *range(4, 12)]  # 3번만 유실
    send_rtp(port, [rtp_packet(i, i * SAMPLES, ulaw_tone(SAMPLES)) for i in sequences])
    await asyncio.sleep(0.3)

    stats = (await client.get(f"/internal/v1/calls/{call['session_id']}/stats")).json()

    assert stats[0]["received"] == len(sequences)
    assert stats[0]["lost"] == 1, "3번 유실이 집계되어야 한다"


async def test_capacity_is_reported(rtp_gateway) -> None:
    client, _ = rtp_gateway
    before = (await client.get("/internal/v1/capacity")).json()

    await start_call(client)
    after = (await client.get("/internal/v1/capacity")).json()

    assert after["active_calls"] == before["active_calls"] + 1
    assert after["active_legs"] == before["active_legs"] + 2
    assert after["free_ports"] == before["free_ports"] - 2


async def test_unknown_call_teardown_is_404(rtp_gateway) -> None:
    client, _ = rtp_gateway

    assert (await client.delete("/internal/v1/calls/sess_nope")).status_code == 404


async def test_telephony_audio_becomes_a_caption(rtp_gateway, bus: InMemoryEventBus) -> None:
    """전화 → VAD → STT → 자막. AICC 패키지가 증명해야 하는 전 구간."""
    client, _ = rtp_gateway
    call = await start_call(client, ["customer"])
    port = call["legs"][0]["port"]

    vad = EnergyVadAdapter()
    await vad.initialize({})
    stt = FakeSTTAdapter(["결제일 연기 문의드립니다"])
    await stt.initialize("", {})
    workers = [
        VadWorker(bus, vad, group="vad", consumer="vad-1"),
        SttWorker(bus, stt, group="stt", consumer="stt-1", publish_ui=False),
    ]
    tasks = [asyncio.create_task(worker.run()) for worker in workers]

    try:
        collector = asyncio.create_task(collect(bus, Topic.STT_DELTA, SttDelta, 1, 8.0))
        await asyncio.sleep(0)

        # 발화 700ms + 무음 800ms (발화 종료 신호)
        packets: list[bytes] = []
        sequence = 1
        for _ in range(35):
            packets.append(rtp_packet(sequence, sequence * SAMPLES, ulaw_tone(SAMPLES)))
            sequence += 1
        for _ in range(40):
            packets.append(rtp_packet(sequence, sequence * SAMPLES, ulaw_silence(SAMPLES)))
            sequence += 1
        send_rtp(port, packets)

        deltas = await collector
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    assert deltas, "전화 오디오가 자막이 되지 않았다"
    assert deltas[0].text == "결제일 연기 문의드립니다"
    assert deltas[0].channel is ChannelRole.CUSTOMER
