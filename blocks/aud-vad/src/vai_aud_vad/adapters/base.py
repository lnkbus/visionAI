"""VAD 어댑터 인터페이스.

STT/TTS와 같은 Model-Agnostic 원칙을 VAD에도 적용한다. 폐쇄망 고객사가 자사
검증을 마친 VAD를 쓰고 싶다고 하면 어댑터 하나만 추가하면 된다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseVadAdapter(ABC):
    """프레임 단위 음성 검출기.

    구현체는 상태를 가질 수 있다(Silero는 RNN 히든 스테이트를 유지한다).
    따라서 어댑터 인스턴스는 **스트림 하나당 하나**를 쓴다 —
    :meth:`clone`으로 세션·채널마다 새 인스턴스를 만든다.
    """

    name: str

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """모델 로드. 프로세스 기동 시 1회."""

    @abstractmethod
    def frame_samples(self, sample_rate: int) -> int:
        """이 검출기가 요구하는 프레임 크기(샘플 수)."""

    @abstractmethod
    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        """PCM 16bit mono 프레임 1개가 음성인지 판정한다."""

    @abstractmethod
    def clone(self) -> BaseVadAdapter:
        """스트림별 독립 상태를 갖는 사본. 모델 가중치는 공유한다."""

    def reset(self) -> None:
        """스트림 종료 시 내부 상태 초기화."""
        return None
