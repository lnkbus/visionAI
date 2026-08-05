"""에너지 기반 VAD — 의존성 없는 기본 어댑터.

GPU도 torch도 없는 환경(CI, 개발 노트북, 소형 PoC)에서 파이프라인 전체를
돌릴 수 있어야 하므로 기본값으로 둔다. 조용한 사무실 녹음에서는 충분히
동작하지만, 콜센터 배경소음에서는 Silero 어댑터를 써야 한다.

적응형 노이즈 플로어를 쓴다: 고정 임계값은 통화마다 다른 입력 게인 때문에
그대로 쓰기 어렵다. 무음 구간의 RMS를 지수이동평균으로 추적하고 그보다
일정 배수 큰 프레임을 음성으로 본다.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from vai_aud_vad.adapters.base import BaseVadAdapter

DEFAULT_FRAME_MS = 30


class EnergyVadAdapter(BaseVadAdapter):
    name = "energy"

    def __init__(
        self,
        *,
        frame_ms: int = DEFAULT_FRAME_MS,
        speech_ratio: float = 3.0,
        floor_alpha: float = 0.05,
        min_rms: float = 120.0,
    ) -> None:
        self.frame_ms = frame_ms
        self.speech_ratio = speech_ratio
        """노이즈 플로어 대비 몇 배면 음성으로 볼지."""
        self.floor_alpha = floor_alpha
        self.min_rms = min_rms
        """절대 하한. 완전 무음(디지털 0) 구간에서 플로어가 0으로 수렴하면
        모든 프레임이 '플로어의 3배'가 되어 버리는 것을 막는다."""
        self._noise_floor = min_rms

    async def initialize(self, config: dict[str, Any]) -> None:
        self.speech_ratio = float(config.get("speech_ratio", self.speech_ratio))
        self.min_rms = float(config.get("min_rms", self.min_rms))
        self._noise_floor = self.min_rms

    def frame_samples(self, sample_rate: int) -> int:
        return int(sample_rate * self.frame_ms / 1000)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if not frame:
            return False
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(samples))))
        threshold = max(self._noise_floor * self.speech_ratio, self.min_rms)
        speech = rms > threshold
        if not speech:
            # 무음 프레임으로만 플로어를 갱신한다. 음성으로 갱신하면
            # 긴 발화 동안 플로어가 따라 올라가 발화를 놓친다.
            self._noise_floor = (1 - self.floor_alpha) * self._noise_floor + self.floor_alpha * rms
        return speech

    def clone(self) -> EnergyVadAdapter:
        twin = EnergyVadAdapter(
            frame_ms=self.frame_ms,
            speech_ratio=self.speech_ratio,
            floor_alpha=self.floor_alpha,
            min_rms=self.min_rms,
        )
        twin._noise_floor = self.min_rms
        return twin

    def reset(self) -> None:
        self._noise_floor = self.min_rms
