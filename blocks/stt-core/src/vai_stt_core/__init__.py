"""STT-CORE — STT 모델 추상화 레이어 블록."""

from vai_stt_core.adapters import BaseSTTAdapter, FakeSTTAdapter, SttResult, create_stt
from vai_stt_core.worker import BLOCK_ID, SttWorker

__all__ = [
    "BLOCK_ID",
    "BaseSTTAdapter",
    "FakeSTTAdapter",
    "SttResult",
    "SttWorker",
    "create_stt",
]
