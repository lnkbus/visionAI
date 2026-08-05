"""AUD-RTP 제어 API — 고객사 CTI/PBX가 통화 시작·종료를 알린다.

이 블록은 미디어만 책임진다. 시그널링(SIP/SIPREC/CTI)은 고객사 환경마다 달라
어댑터로 두고, 여기서는 그 어댑터가 호출할 제어 계약을 고정한다.

통화 시작 → 세션 생성(CORE-BUS) → 포트 할당 → 수신 시작
통화 종료 → 수신 중단 → 버퍼 flush → 세션 종료(요약 트리거)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_aud_rtp.calls import CallRegistry, PortPool, PortPoolExhausted
from vai_aud_rtp.receiver import RtpReceiver
from vai_aud_rtp.rtp import JitterBufferConfig
from vai_common.bus import EventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.session import AudioFormat, ChannelRole, SessionCreate, SessionProfile

log = logging.getLogger(__name__)
BLOCK_ID = "AUD-RTP"


class RtpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_RTP_", extra="ignore")

    bind_host: str = "0.0.0.0"
    port_start: int = 40000
    port_end: int = 40100
    """포트 쌍 하나가 채널 하나다. 100포트 = 동시 25통화(고객+상담원 = 2레그,
    RTCP용 홀수 포트를 비워 두므로)."""

    jitter_depth: int = 3
    """20ms 패킷 기준 60ms 버퍼. AICC는 초저지연이 목표라 얕게 잡는다."""

    idle_timeout_s: float = 120.0
    """양쪽 미디어가 이만큼 조용하면 통화를 회수한다.

    **이 값이 없으면 포트는 명시적 DELETE로만 회수된다.** 시그널링 연동은
    고객사마다 다르고(SIPREC/게이트웨이/CTI) 이 저장소 밖에 있다 — BYE를
    한 번 놓칠 때마다 포트 두 개가 영구히 사라지고, 기본 풀에서는 그런
    통화 25건이면 신규 인입이 통째로 막힌다.

    RTP는 20ms마다 온다. 2분 침묵은 미디어 경로가 사라졌다는 뜻이지만,
    보류(hold) 중 무음 억제로 송신을 멈추는 게이트웨이가 있어 짧게 잡으면
    멀쩡한 통화를 끊는다. 현장 게이트웨이 동작을 확인한 뒤 조정한다."""

    reap_interval_s: float = 15.0


class StartCallRequest(BaseModel):
    tenant_id: str
    profile: SessionProfile = SessionProfile.AICC
    channels: list[ChannelRole] = Field(
        default_factory=lambda: [ChannelRole.CUSTOMER, ChannelRole.AGENT]
    )
    external_call_id: str = ""
    """고객사 CTI의 통화 식별자. 장애 조사 시 양쪽 로그를 맞추는 열쇠다."""


class CallLegInfo(BaseModel):
    channel: ChannelRole
    port: int


class StartCallResponse(BaseModel):
    session_id: str
    legs: list[CallLegInfo]
    """CTI는 이 포트로 미디어를 보내면 된다."""


class CallStats(BaseModel):
    session_id: str
    channel: ChannelRole
    port: int
    received: int
    lost: int
    late: int
    reordered: int
    duplicated: int
    resynced: int


def create_app(
    bus: EventBus | None = None, core_bus_client: httpx.AsyncClient | None = None
) -> FastAPI:
    common = get_settings()
    cfg = RtpSettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        event_bus = bus or build_bus(common.redis_url)
        registry = CallRegistry(PortPool(cfg.port_start, cfg.port_end))
        application.state.bus = event_bus
        application.state.registry = registry
        application.state.receiver = RtpReceiver(event_bus, registry, host=cfg.bind_host)
        application.state.core_bus = core_bus_client or httpx.AsyncClient(
            base_url=common.core_bus_url.rstrip("/"), timeout=3.0
        )
        log.info(
            "RTP 게이트웨이 기동",
            extra={
                "port_range": f"{cfg.port_start}-{cfg.port_end}",
                "idle_timeout_s": cfg.idle_timeout_s,
            },
        )
        reaper = asyncio.create_task(
            _reap_idle_calls(application, cfg.idle_timeout_s, cfg.reap_interval_s),
            name="rtp-idle-reaper",
        )
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            await application.state.receiver.close_all()
            if not injected:
                await application.state.core_bus.aclose()
                await event_bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI AUD-RTP", settings=common, lifespan=lifespan
    )

    @app.post(
        "/internal/v1/calls",
        response_model=StartCallResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["calls"],
    )
    async def start_call(payload: StartCallRequest, request: Request) -> StartCallResponse:
        """통화 시작. 세션을 만들고 채널별 RTP 포트를 열어 돌려준다."""
        if not payload.channels:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="채널이 필요하다")

        session = await _create_session(request, payload)
        registry: CallRegistry = request.app.state.registry
        try:
            legs = registry.open(
                session.session_id,
                payload.tenant_id,
                payload.profile,
                payload.channels,
                buffer_config=JitterBufferConfig(depth=cfg.jitter_depth),
            )
        except PortPoolExhausted as exc:
            # 세션만 만들어 두고 미디어를 못 받으면 빈 상담 기록이 남는다.
            await _close_session(request, session.session_id)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="가용 RTP 포트가 없다"
            ) from exc

        receiver: RtpReceiver = request.app.state.receiver
        for leg in legs:
            await receiver.open_leg(leg)

        log.info(
            "통화 시작",
            extra={
                "session_id": session.session_id,
                "external_call_id": payload.external_call_id,
                "tenant_id": payload.tenant_id,
            },
        )
        return StartCallResponse(
            session_id=session.session_id,
            legs=[CallLegInfo(channel=leg.channel, port=leg.port) for leg in legs],
        )

    @app.delete("/internal/v1/calls/{session_id}", tags=["calls"])
    async def end_call(session_id: str, request: Request) -> dict[str, str]:
        """통화 종료. 버퍼를 비우고 세션을 닫아 요약을 트리거한다."""
        registry: CallRegistry = request.app.state.registry
        receiver: RtpReceiver = request.app.state.receiver

        legs = registry.legs_for_session(session_id)
        if not legs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="통화를 찾을 수 없다")

        for leg in legs:
            await receiver.close_leg(leg)
        registry.close(session_id)
        await _close_session(request, session_id)
        return {"status": "closed", "session_id": session_id}

    @app.get(
        "/internal/v1/calls/{session_id}/stats", response_model=list[CallStats], tags=["calls"]
    )
    async def call_stats(session_id: str, request: Request) -> list[CallStats]:
        """레그별 수신 품질. 오인식 조사 시 "망 문제인가 모델 문제인가"를 가른다."""
        registry: CallRegistry = request.app.state.registry
        legs = registry.legs_for_session(session_id)
        if not legs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="통화를 찾을 수 없다")
        return [
            CallStats(
                session_id=session_id,
                channel=leg.channel,
                port=leg.port,
                received=leg.buffer.stats.received,
                lost=leg.buffer.stats.lost,
                late=leg.buffer.stats.late,
                reordered=leg.buffer.stats.reordered,
                duplicated=leg.buffer.stats.duplicated,
                resynced=leg.buffer.stats.resynced,
            )
            for leg in legs
        ]

    @app.get("/internal/v1/capacity", tags=["calls"])
    async def capacity(request: Request) -> dict[str, int]:
        """남은 수용량. 라이선스 동시 채널 수와 함께 감시한다."""
        registry: CallRegistry = request.app.state.registry
        return {
            "active_calls": registry.active_calls,
            "active_legs": registry.active_legs,
            "free_ports": registry.free_ports,
        }

    return app


async def _create_session(request: Request, payload: StartCallRequest):  # type: ignore[no-untyped-def]
    client: httpx.AsyncClient = request.app.state.core_bus
    body = SessionCreate(
        tenant_id=payload.tenant_id,
        profile=payload.profile,
        # 전화망은 8kHz G.711이다.
        audio_format=AudioFormat.PCM_8K,
        metadata={"external_call_id": payload.external_call_id} if payload.external_call_id else {},
    )
    try:
        response = await client.post("/internal/v1/sessions", json=body.model_dump(mode="json"))
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="세션 서비스에 연결할 수 없다"
        ) from exc

    from vai_contracts.session import Session

    return Session.model_validate(response.json())


async def _reap_idle_calls(app: FastAPI, timeout_s: float, interval_s: float) -> None:
    """미디어가 멈춘 통화를 회수한다.

    끊긴 통화를 아무도 알려 주지 않는 상황은 예외가 아니라 **기본값**이다 —
    시그널링 연동이 이 저장소 밖에 있기 때문이다. 회수하지 않으면 포트가
    영구히 새고, 결국 신규 인입이 막힌다.

    세션도 함께 닫는다. 미디어만 접고 세션을 남기면 통화가 영영 '진행 중'으로
    남아 요약도 나오지 않는다.
    """
    while True:
        await asyncio.sleep(interval_s)
        registry: CallRegistry = app.state.registry
        receiver: RtpReceiver = app.state.receiver
        for session_id in registry.idle_sessions(timeout_s):
            legs = registry.legs_for_session(session_id)
            log.warning(
                "미디어가 멈춘 통화 회수 — 종료 신호를 못 받았다",
                extra={
                    "session_id": session_id,
                    "idle_s": round(max(leg.idle_for() for leg in legs), 1),
                    "legs": len(legs),
                },
            )
            for leg in legs:
                await receiver.close_leg(leg)
            registry.close(session_id)
            await _close_session_via(app.state.core_bus, session_id)


async def _close_session_via(client: httpx.AsyncClient, session_id: str) -> None:
    try:
        await client.post(f"/internal/v1/sessions/{session_id}/close")
    except httpx.HTTPError:
        log.warning("세션 종료 실패", extra={"session_id": session_id}, exc_info=True)


async def _close_session(request: Request, session_id: str) -> None:
    # 세션 종료 실패로 통화 종료 API를 실패시키지 않는다. 미디어는 이미
    # 끊겼고, 세션은 TTL로 정리된다.
    await _close_session_via(request.app.state.core_bus, session_id)
