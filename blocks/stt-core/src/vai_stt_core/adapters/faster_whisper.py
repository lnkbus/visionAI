"""Faster-Whisper 어댑터 — 사양서 지정 기본 엔진.

폐쇄망 전제이므로 ``model_path``는 반드시 로컬 경로다(모델 허브 조회는 실패한다).
번들 생성 시 GPU 프로파일에 맞는 모델을 함께 반입한다.

추론은 GPU를 점유하는 동기 호출이라 스레드로 넘긴다. 이벤트 루프에서 직접
돌리면 같은 워커의 다른 세션이 전부 멈춘다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import numpy as np

from vai_stt_core.adapters.base import BaseSTTAdapter, SttResult

log = logging.getLogger(__name__)


class FasterWhisperAdapter(BaseSTTAdapter):
    name = "faster_whisper"

    def __init__(self) -> None:
        self._model: Any = None
        self._options: dict[str, Any] = {}
        self._language: str | None = None

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        from faster_whisper import WhisperModel

        device = config.get("device", "cuda")
        compute_type = config.get("compute_type", "float16" if device == "cuda" else "int8")
        self._language = config.get("language") or None
        """None이면 자동 감지 — 다국어 요건상 기본값으로 둔다."""

        self._model = await asyncio.to_thread(
            WhisperModel,
            model_path,
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
        self._options = {
            "beam_size": int(config.get("beam_size", 1)),
            "vad_filter": False,  # 구간 분할은 AUD-VAD가 이미 했다
            "condition_on_previous_text": False,
        }
        log.info(
            "Faster-Whisper 로드",
            extra={"model_path": model_path, "device": device, "compute_type": compute_type},
        )

    async def transcribe_stream(
        self, audio_chunk: bytes, sample_rate: int = 16000
    ) -> AsyncGenerator[SttResult, None]:
        if self._model is None:
            raise RuntimeError("initialize()를 먼저 호출해야 한다")
        if not audio_chunk:
            return

        audio = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = await asyncio.to_thread(self._run, audio, sample_rate)
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            yield SttResult(
                text=text,
                is_final=True,
                confidence=_to_confidence(segment.avg_logprob),
                speaker_id=None,
                language=info.language or self._language or "ko",
            )

    def _run(self, audio: np.ndarray, sample_rate: int) -> tuple[list[Any], Any]:
        if sample_rate != 16000:
            audio = _resample_to_16k(audio, sample_rate)
        segments, info = self._model.transcribe(audio, language=self._language, **self._options)
        return list(segments), info

    async def close(self) -> None:
        self._model = None


def _to_confidence(avg_logprob: float) -> float:
    """평균 로그확률을 0~1 신뢰도로 환산한다.

    Whisper는 확률을 직접 주지 않는다. exp(avg_logprob)가 통상적인 근사치이며,
    임계값 튜닝은 도메인 평가셋으로 별도 수행한다(docs/06 §5).
    """
    return float(min(1.0, max(0.0, np.exp(avg_logprob))))


def _resample_to_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """선형 보간 리샘플링.

    AICC의 8kHz G.711 입력을 Whisper가 요구하는 16kHz로 올린다. 8k→16k는
    정수배라 선형 보간으로도 품질 손실이 실질적으로 없다.
    """
    if sample_rate == 16000:
        return audio
    ratio = 16000 / sample_rate
    target_len = int(len(audio) * ratio)
    if target_len <= 0:
        return audio
    src_idx = np.linspace(0, len(audio) - 1, num=target_len, dtype=np.float32)
    resampled = np.interp(src_idx, np.arange(len(audio), dtype=np.float32), audio)
    return np.asarray(resampled, dtype=np.float32)
