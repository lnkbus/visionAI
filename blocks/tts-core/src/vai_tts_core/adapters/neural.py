"""신경망 TTS 어댑터 — CosyVoice · Kokoro (사양서 §1).

폐쇄망 번들에 가중치를 함께 반입한다. 모델이 없으면 기동 시점에 실패해야 하며,
조용히 무음을 내면 안 된다 — 통화는 연결됐는데 아무 소리도 안 나는 상태는
장애로 인식되지 않아 원인 파악이 가장 오래 걸린다.

두 엔진의 API가 다르므로 어댑터를 나눈다. 공통 부분(정규화·문장 분할·재표본화)은
전부 바깥에 있어 엔진 교체가 그쪽으로 번지지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from vai_contracts.speech import VoiceProfile
from vai_tts_core.adapters.base import BaseTTSAdapter

log = logging.getLogger(__name__)


class CosyVoiceAdapter(BaseTTSAdapter):
    """CosyVoice 계열. 화자 복제(zero-shot)를 지원해 고객사 전용 목소리를 만든다."""

    name = "cosyvoice"
    native_sample_rate = 22050

    def __init__(self) -> None:
        self._model: Any = None
        self._prompts: dict[str, Any] = {}

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        from cosyvoice.cli.cosyvoice import CosyVoice

        self._model = await asyncio.to_thread(CosyVoice, model_path)
        self.native_sample_rate = int(config.get("sample_rate", self.native_sample_rate))
        log.info("CosyVoice 로드", extra={"model_path": model_path})

    async def synthesize_stream(
        self, text: str, voice: VoiceProfile
    ) -> AsyncGenerator[bytes, None]:
        import numpy as np

        def run() -> list[bytes]:
            frames: list[bytes] = []
            for output in self._model.inference_sft(text, voice.voice_id, stream=True):
                audio = output["tts_speech"].numpy().flatten()
                frames.append((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())
            return frames

        # 엔진이 동기 제너레이터라 스레드로 넘긴다. 여기서 이벤트 루프를 막으면
        # 같은 프로세스의 다른 세션 합성이 전부 멈춘다.
        for frame in await asyncio.to_thread(run):
            yield frame

    async def close(self) -> None:
        self._model = None


class KokoroAdapter(BaseTTSAdapter):
    """Kokoro 계열. 가볍고 빨라 CPU만 있는 소형 PoC에 쓴다."""

    name = "kokoro"
    native_sample_rate = 24000

    def __init__(self) -> None:
        self._pipeline: Any = None

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        from kokoro import KPipeline

        self._pipeline = await asyncio.to_thread(
            KPipeline, lang_code=config.get("lang_code", "k"), repo_id=model_path
        )
        self.native_sample_rate = int(config.get("sample_rate", self.native_sample_rate))

    async def synthesize_stream(
        self, text: str, voice: VoiceProfile
    ) -> AsyncGenerator[bytes, None]:
        import numpy as np

        def run() -> list[bytes]:
            frames: list[bytes] = []
            for _, _, audio in self._pipeline(text, voice=voice.voice_id, speed=voice.speed):
                array = np.asarray(audio, dtype="float32").flatten()
                frames.append((np.clip(array, -1.0, 1.0) * 32767).astype("<i2").tobytes())
            return frames

        for frame in await asyncio.to_thread(run):
            yield frame

    async def close(self) -> None:
        self._pipeline = None
