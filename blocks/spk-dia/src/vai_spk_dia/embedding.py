"""화자 임베딩 어댑터.

발화 구간에서 "누가 말했는가"를 나타내는 벡터를 뽑는다. STT/TTS/sLLM과 같은
Model-Agnostic 원칙을 적용해, 폐쇄망 GPU 프로파일에서는 신경망 임베더를,
CI·소형 PoC에서는 모델 없이 도는 스펙트럼 임베더를 쓴다.

기본값 :class:`SpectralEmbedder`는 **성별·음역이 뚜렷이 다른 화자만 가른다.**
같은 성별의 비슷한 목소리는 구분하지 못한다. 회의록 품질이 필요한 고객사는
반드시 신경망 임베더(ECAPA-TDNN 계열)를 쓴다 — 이 한계를 문서에도 적어 둔다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

MIN_SAMPLES = 400
"""임베딩을 뽑을 최소 길이. 너무 짧으면 특징이 잡음에 묻힌다."""


class BaseSpeakerEmbedder(ABC):
    name: str
    dimension: int

    @abstractmethod
    async def initialize(self, model_path: str, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def embed(self, pcm: bytes, sample_rate: int) -> np.ndarray | None:
        """PCM16 구간에서 화자 임베딩(L2 정규화)을 뽑는다.

        구간이 너무 짧거나 무음이면 ``None``. 호출부는 그 구간의 화자 판정을
        건너뛴다 — 근거 없는 라벨을 붙이면 회의록에 없는 참석자가 생긴다.
        """

    async def close(self) -> None:
        return None


def _normalize(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    if norm == 0 or not np.isfinite(norm):
        return None
    result: np.ndarray = (vector / norm).astype(np.float32)
    return result


class SpectralEmbedder(BaseSpeakerEmbedder):
    """스펙트럼 통계 기반 임베더. 모델·GPU·네트워크가 필요 없다.

    멜 유사 대역별 로그 에너지의 평균과 표준편차를 이어 붙인다. 성도(vocal
    tract) 특성의 거친 윤곽을 담으므로 음역이 다른 화자는 갈리지만, 같은
    음역의 두 사람은 섞인다.
    """

    name = "spectral"

    def __init__(self, bands: int = 24) -> None:
        self.bands = bands
        self.dimension = bands * 2

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        self.bands = int(config.get("bands", self.bands))
        self.dimension = self.bands * 2

    async def embed(self, pcm: bytes, sample_rate: int) -> np.ndarray | None:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if samples.size < MIN_SAMPLES:
            return None

        frame = 512
        hop = 256
        if samples.size < frame:
            return None

        window = np.hanning(frame).astype(np.float32)
        frames = [
            samples[start : start + frame] * window for start in range(0, samples.size - frame, hop)
        ]
        if not frames:
            return None

        spectrum = np.abs(np.fft.rfft(np.stack(frames), axis=1))
        # 저역에 촘촘하고 고역에 성긴 대역 — 사람 목소리의 변별 정보가 저역에 몰려 있다.
        edges = np.geomspace(1, spectrum.shape[1] - 1, self.bands + 1).astype(int)
        energies = np.stack(
            [
                np.log1p(spectrum[:, edges[i] : max(edges[i] + 1, edges[i + 1])].mean(axis=1))
                for i in range(self.bands)
            ],
            axis=1,
        )
        if not np.isfinite(energies).all():
            return None

        # 평균은 음색, 표준편차는 억양의 변화 폭을 담는다.
        feature = np.concatenate([energies.mean(axis=0), energies.std(axis=0)])
        # 발화 크기(마이크 거리)의 영향을 줄이려고 중심화한 뒤 정규화한다.
        return _normalize(feature - feature.mean())


class SpeechBrainEmbedder(BaseSpeakerEmbedder):
    """ECAPA-TDNN 계열 신경망 임베더. 폐쇄망 번들에 가중치를 함께 반입한다."""

    name = "speechbrain"

    def __init__(self) -> None:
        self.dimension = 0
        self._model: Any = None

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        import asyncio
        from pathlib import Path

        # **먼저 경로부터 본다.** 없는 경로를 넘기면 speechbrain 이 모델 허브로
        # 나가려다 죽고, 로그에는 그 URL 이 찍힌다 — 폐쇄망 담당자가 그 문구를
        # 보고 할 수 있는 일이 없다.
        # 디스크 조회는 스레드로 넘긴다 — 기동 중 한 번뿐이지만, 이벤트 루프
        # 안에서 블로킹 호출을 하는 습관은 남기지 않는다.
        if not await asyncio.to_thread(Path(model_path).is_dir):
            raise RuntimeError(
                f"화자분리 임베더 모델이 없다: {model_path}\n"
                "  네트워크가 있는 곳에서 먼저 받는다:\n"
                "    deploy/airgap/fetch_models.sh --out models --spk --no-stt\n"
                "  모델 없이 돌리려면 VAI_DIA_EMBEDDER=spectral 로 내린다 — "
                "다만 spectral 은 음역이 뚜렷이 다른 화자만 가른다."
            )

        from speechbrain.inference.speaker import EncoderClassifier

        self._model = await asyncio.to_thread(
            EncoderClassifier.from_hparams,
            source=model_path,
            run_opts={"device": config.get("device", "cpu")},
        )
        self.dimension = int(config.get("dimension", 192))

    async def embed(self, pcm: bytes, sample_rate: int) -> np.ndarray | None:
        import asyncio

        import torch

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < MIN_SAMPLES:
            return None

        def run() -> np.ndarray:
            with torch.no_grad():
                tensor = torch.from_numpy(samples).unsqueeze(0)
                encoded = self._model.encode_batch(tensor).squeeze().cpu().numpy()
                return np.asarray(encoded, dtype=np.float32)

        vector = await asyncio.to_thread(run)
        return _normalize(np.asarray(vector, dtype=np.float32))

    async def close(self) -> None:
        self._model = None


def _load_speechbrain() -> BaseSpeakerEmbedder:
    return SpeechBrainEmbedder()


_REGISTRY: dict[str, Callable[[], BaseSpeakerEmbedder]] = {
    "spectral": SpectralEmbedder,
    "speechbrain": _load_speechbrain,
}


def create_embedder(name: str) -> BaseSpeakerEmbedder:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 화자 임베더 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None
