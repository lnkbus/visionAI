"""TTS-CORE — 실시간 음성 합성 블록.

버스 경로(``tts.request`` → ``tts.audio``)가 본선이고, HTTP는 저작·시험용이다.
저작 도구가 "이 문장이 어떻게 읽히는지"를 확인할 수 있어야 고객사가 스스로
안내 문구를 다듬는다 — 그게 안 되면 문구 한 줄 바꾸는 데 공급사 인력이 든다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import EventBus, RedisEventBus, build_bus
from vai_common.config_store import (
    CachedConfig,
    ConfigKind,
    ConfigStore,
    InMemoryConfigStore,
    RedisConfigStore,
)
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_common.worker import BlockWorker
from vai_contracts.authoring import TtsLexicon
from vai_contracts.speech import SpeechCancel, VoiceProfile
from vai_contracts.topics import Topic
from vai_tts_core.adapters import available, create_adapter
from vai_tts_core.normalize import normalize
from vai_tts_core.readings import ReadingCache
from vai_tts_core.streaming import split_for_streaming
from vai_tts_core.synth import Synthesizer
from vai_tts_core.worker import BLOCK_ID, CancelWorker, SynthesisWorker

log = logging.getLogger(__name__)


class TtsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_TTS_", extra="ignore")

    adapter: str = "fake"
    model_path: str = ""
    adapter_config: str = "{}"
    workers: int = 2
    """합성은 GPU를 오래 붙잡는다. 워커가 하나면 한 통화의 긴 안내가
    다른 통화의 첫 소리를 통째로 밀어낸다."""

    lexicon_enabled: bool = True
    """읽기 사전 사용 여부. 배포된 사전이 없으면 자동으로 무시된다."""

    lexicon_ttl_s: float = 30.0


class PreviewRequest(BaseModel):
    text: str
    voice: VoiceProfile = Field(default_factory=VoiceProfile)
    tenant_id: str = ""
    """읽기 사전을 어느 테넌트 것으로 볼지. 비면 사전 없이 규칙만 적용한다."""


class PreviewResult(BaseModel):
    """저작 도구가 보는 결과. 오디오 없이 **읽는 방식만** 먼저 확인한다."""

    original: str
    normalized: str
    chunks: list[str]
    first_chunk_chars: int
    readings_applied: int = 0
    """적용된 사전 항목 수. 0이면 **규칙만 돌았다는 뜻**이다 — 등록했는데
    안 읽히는 경우와 애초에 사전이 없는 경우를 화면이 구분할 수 있어야 한다."""


def create_app(synthesizer: Synthesizer | None = None, bus: EventBus | None = None) -> FastAPI:
    common = get_settings()
    cfg = TtsSettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        synth = synthesizer
        if synth is None:
            adapter = create_adapter(cfg.adapter)
            await adapter.initialize(cfg.model_path, json.loads(cfg.adapter_config))
            synth = Synthesizer(adapter)
            log.info("TTS 어댑터 로드", extra={"adapter": cfg.adapter})
        application.state.synth = synth
        application.state.bus = bus or build_bus(common.redis_url)

        readings: ReadingCache | None = None
        config_store: ConfigStore | None = None
        if cfg.lexicon_enabled:
            active_bus = application.state.bus
            config_store = (
                RedisConfigStore(active_bus.redis)
                if isinstance(active_bus, RedisEventBus)
                else InMemoryConfigStore()
            )
            readings = ReadingCache(
                CachedConfig(
                    config_store, ConfigKind.TTS_LEXICON, TtsLexicon, ttl_s=cfg.lexicon_ttl_s
                )
            )
        application.state.readings = readings

        workers: list[BlockWorker[Any]] = [
            SynthesisWorker(
                application.state.bus,
                synth,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                readings=readings,
            )
            for i in range(max(1, cfg.workers))
        ]
        # 취소는 별도 그룹이다. 같은 그룹이면 취소가 합성 중인 워커의 큐 뒤에
        # 줄을 서고, 발화가 끝난 뒤에야 도착한다 — 그때 취소하는 것은 취소가 아니다.
        workers.append(
            CancelWorker(
                application.state.bus,
                synth,
                group=f"{common.consumer_group}-cancel",
                consumer=common.consumer_name,
            )
        )
        tasks = [asyncio.create_task(w.run(), name=f"tts-{i}") for i, w in enumerate(workers)]
        if readings is not None and config_store is not None:
            # 배포 알림을 받으면 캐시 TTL 을 기다리지 않고 즉시 새 사전을 쓴다.
            tasks.append(
                asyncio.create_task(
                    _watch_readings(config_store, readings), name="tts-lexicon-watch"
                )
            )
        try:
            yield
        finally:
            for worker in workers:
                worker.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if synthesizer is None:
                await synth.adapter.close()
            if not injected:
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI TTS-CORE", settings=common, lifespan=lifespan
    )

    @app.post("/internal/v1/tts/preview", response_model=PreviewResult, tags=["tts"])
    async def preview(payload: PreviewRequest, request: Request) -> PreviewResult:
        """읽는 방식 미리보기 — 합성 없이 텍스트만 본다.

        "50,000원"이 "오만 원"으로 읽히는지 확인하는 데 GPU를 쓸 이유가 없다.
        저작 화면이 타이핑하는 동안 호출한다.
        """
        cache: ReadingCache | None = getattr(request.app.state, "readings", None)
        readings = await cache.readings(payload.tenant_id) if cache and payload.tenant_id else {}
        normalized = normalize(payload.text, readings=readings)
        chunks = split_for_streaming(normalized)
        return PreviewResult(
            original=payload.text,
            normalized=normalized,
            chunks=chunks,
            first_chunk_chars=len(chunks[0]) if chunks else 0,
            readings_applied=sum(1 for surface in readings if surface in payload.text),
        )

    @app.post("/internal/v1/tts/synthesize", tags=["tts"])
    async def synthesize(payload: PreviewRequest, request: Request) -> Response:
        """전체 합성(WAV). 저작 도구의 '들어 보기'용이며 상담 경로가 아니다.

        상담은 버스로 흐른다 — HTTP로 전체를 기다리면 스트리밍이 무의미하다.
        """
        synth: Synthesizer = request.app.state.synth
        if not payload.text.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="텍스트가 비어 있다")

        cache: ReadingCache | None = getattr(request.app.state, "readings", None)
        readings = await cache.readings(payload.tenant_id) if cache and payload.tenant_id else None
        frames: list[bytes] = []
        async for segment, _ in synth.synthesize(
            "preview", "preview", payload.text, payload.voice, readings=readings
        ):
            frames.append(segment.pcm)
        return Response(
            content=_wav(b"".join(frames), payload.voice.sample_rate),
            media_type="audio/wav",
        )

    @app.post("/internal/v1/tts/cancel", tags=["tts"])
    async def cancel(payload: SpeechCancel, request: Request) -> dict[str, int]:
        """끼어들기를 버스로 흘린다. AUD-VAD가 발화 시작을 감지하면 호출한다."""
        bus_: EventBus = request.app.state.bus
        await bus_.publish(Topic.TTS_CANCEL, payload)
        return {"published": 1}

    @app.get("/internal/v1/tts/voices", tags=["tts"])
    async def voices(request: Request) -> dict[str, object]:
        synth: Synthesizer = request.app.state.synth
        return {
            "adapters": available(),
            "active": synth.adapter.name,
            "native_sample_rate": synth.adapter.native_sample_rate,
            "active_turns": synth.registry.active,
        }

    return app


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    """WAV 헤더를 붙인다. 브라우저가 바로 재생할 수 있어야 저작 도구가 성립한다."""
    import struct

    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


async def _watch_readings(store: ConfigStore, cache: ReadingCache) -> None:
    """사전 배포 알림 구독.

    알림을 놓쳐도 캐시 TTL 이 만료되면 따라잡는다 — 이 태스크가 죽어도
    동작이 깨지지는 않고 반영이 늦어질 뿐이다.
    """
    try:
        async for kind, tenant_id in store.watch():
            if kind is ConfigKind.TTS_LEXICON:
                cache.invalidate(tenant_id)
                log.info("TTS 읽기 사전 갱신 알림", extra={"tenant_id": tenant_id})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("TTS 사전 알림 구독 중단 — TTL 갱신으로 계속 동작한다")
