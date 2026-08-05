"""SPK-DIA — 화자분리 블록."""

from vai_spk_dia.diarizer import Assignment, DiarizerConfig, OnlineDiarizer, Speaker
from vai_spk_dia.embedding import BaseSpeakerEmbedder, SpectralEmbedder, create_embedder
from vai_spk_dia.worker import BLOCK_ID, DiarizationWorker

__all__ = [
    "BLOCK_ID",
    "Assignment",
    "BaseSpeakerEmbedder",
    "DiarizationWorker",
    "DiarizerConfig",
    "OnlineDiarizer",
    "Speaker",
    "SpectralEmbedder",
    "create_embedder",
]
