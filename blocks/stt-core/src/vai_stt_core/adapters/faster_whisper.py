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


def cuda_device_count() -> int:
    """쓸 수 있는 CUDA 장치 수. 엔진이 없으면 0으로 본다."""
    try:
        import ctranslate2
    except ImportError:  # pragma: no cover - 엔진 없는 이미지
        return 0
    try:
        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # 드라이버·라이브러리 문제 원인은 여러 가지다 — 가리지 않는다
        log.warning("CUDA 조회 실패 — 없는 것으로 본다", exc_info=True)
        return 0


def resolve_device(requested: str) -> str:
    """요청한 장치를 실제로 쓸 수 있는 것으로 바꾼다.

    ``auto``  — 있으면 GPU, 없으면 CPU. **어느 쪽으로 갔는지 로그에 남긴다.**
    ``cuda``  — 없으면 **거부한다.** 조용히 CPU로 내려가면 "제안서에는 GPU라고
                썼는데 현장은 CPU"가 되고, 그 사실은 아무 데도 안 적힌다.
    ``cpu``   — 그대로.

    맥에서는 NVIDIA GPU가 꽂혀 있어도 컨테이너가 쓸 수 없다. Docker Desktop 은
    리눅스 VM 안에서 돌고 GPU 통과 경로가 없으며, macOS 용 CUDA 도 끊겼다.
    그래서 여기서 미리 막지 않으면 시연 자리에서 크래시 루프로 드러난다.
    """
    wanted = requested.strip().lower() or "cuda"
    if wanted not in {"auto", "cpu", "cuda"}:
        # 오타를 그대로 넘기면 엔진이 훨씬 뒤에서 알아보기 어려운 오류를 낸다.
        raise ValueError(f'device 는 auto·cpu·cuda 중 하나다 (받은 값: "{requested}")')
    if wanted == "cpu":
        return "cpu"

    available = cuda_device_count()
    if wanted == "auto":
        device = "cuda" if available else "cpu"
        log.info(
            "STT 장치 자동 선택",
            extra={"device": device, "cuda_devices": available},
        )
        return device

    if wanted == "cuda" and not available:
        raise RuntimeError(
            "device=cuda 인데 쓸 수 있는 CUDA 장치가 없다. "
            "맥(Docker Desktop)에서는 NVIDIA GPU 가 꽂혀 있어도 컨테이너가 쓸 수 없다 — "
            "VM 안에서 돌고 GPU 통과 경로가 없다. "
            'CPU 로 돌리려면 VAI_STT_ADAPTER_CONFIG 의 device 를 "cpu" 로, '
            '장비에 따라 자동으로 고르려면 "auto" 로 둔다.'
        )
    return wanted


class FasterWhisperAdapter(BaseSTTAdapter):
    name = "faster_whisper"

    def __init__(self) -> None:
        self._model: Any = None
        self._options: dict[str, Any] = {}
        self._language: str | None = None

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        from faster_whisper import WhisperModel

        device = resolve_device(str(config.get("device", "cuda")))
        compute_type = config.get("compute_type") or ("float16" if device == "cuda" else "int8")
        self._language = config.get("language") or None
        """None이면 자동 감지 — 다국어 요건상 기본값으로 둔다."""

        # CPU 추론에서는 스레드 수가 곧 처리량이다. 0이면 엔진 기본값(코어 수)에
        # 맡긴다. 한 장비에 워커를 여럿 띄울 때는 **나눠 줘야** 서로 코어를
        # 뺏지 않는다 — 안 나누면 워커를 늘릴수록 느려지는 구간이 생긴다.
        cpu_threads = int(config.get("cpu_threads", 0) or 0)

        self._model = await asyncio.to_thread(
            WhisperModel,
            model_path,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            local_files_only=True,
        )
        self._options = {
            "beam_size": int(config.get("beam_size", 1)),
            "vad_filter": False,  # 구간 분할은 AUD-VAD가 이미 했다
            "condition_on_previous_text": False,
        }
        log.info(
            "Faster-Whisper 로드",
            extra={
                "model_path": model_path,
                "device": device,
                "compute_type": compute_type,
                "cpu_threads": cpu_threads or "자동",
            },
        )

    async def transcribe_stream(
        self, audio_chunk: bytes, sample_rate: int = 16000, *, hint: str = ""
    ) -> AsyncGenerator[SttResult, None]:
        if self._model is None:
            raise RuntimeError("initialize()를 먼저 호출해야 한다")
        if not audio_chunk:
            return

        audio = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = await asyncio.to_thread(self._run, audio, sample_rate, hint)
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

    def _run(self, audio: np.ndarray, sample_rate: int, hint: str) -> tuple[list[Any], Any]:
        if sample_rate != 16000:
            audio = _resample_to_16k(audio, sample_rate)
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            # 커스텀 사전의 상품명·전문용어를 디코딩 힌트로 넣는다. 후처리 교정이
            # 등록된 변형만 잡는 데 비해, 이쪽은 미등록 변형에도 도움이 된다.
            initial_prompt=hint or None,
            **self._options,
        )
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
