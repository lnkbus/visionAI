"""Silero VAD 어댑터 — 사양서 지정 엔진.

torch 의존성은 선택적 extra(`vai-aud-vad[silero]`)로 둔다. 폐쇄망 번들에는
모델 가중치를 함께 반입하므로 ``model_path``로 로컬 경로를 지정할 수 있어야
한다(허브 다운로드는 폐쇄망에서 실패한다).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from vai_aud_vad.adapters.base import BaseVadAdapter

# Silero는 16k에서 512, 8k에서 256 샘플 프레임만 받는다.
_FRAME_SAMPLES = {8000: 256, 16000: 512}


class SileroVadAdapter(BaseVadAdapter):
    name = "silero"

    def __init__(self, *, threshold: float = 0.5) -> None:
        self.threshold = threshold
        self._model: Any = None

    async def initialize(self, config: dict[str, Any]) -> None:
        import torch

        self.threshold = float(config.get("threshold", self.threshold))
        model_path = config.get("model_path")
        if model_path:
            self._model = torch.jit.load(model_path)
        else:
            from silero_vad import load_silero_vad

            self._model = load_silero_vad()
        self._model.eval()

    def frame_samples(self, sample_rate: int) -> int:
        try:
            return _FRAME_SAMPLES[sample_rate]
        except KeyError:
            raise ValueError(f"Silero VAD는 8k/16k만 지원한다: {sample_rate}") from None

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        import torch

        if self._model is None:
            raise RuntimeError("initialize()를 먼저 호출해야 한다")
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        with torch.no_grad():
            prob = self._model(torch.from_numpy(samples), sample_rate).item()
        return bool(prob >= self.threshold)

    def clone(self) -> SileroVadAdapter:
        import copy

        twin = SileroVadAdapter(threshold=self.threshold)
        # 모델을 공유하면 RNN 히든 스테이트도 공유되어 채널끼리 서로의 발화를
        # 오염시킨다. Silero는 1MB 남짓이라 스트림당 사본이 더 안전하고 싸다.
        twin._model = copy.deepcopy(self._model) if self._model is not None else None
        twin.reset()
        return twin

    def reset(self) -> None:
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()
