"""TTS 모델 추상화 — 사양서 §1의 Model-Agnostic 원칙.

STT와 같은 구조다. 서비스 로직은 이 ABC와 레지스트리만 알고, 엔진 교체가
합성 로직 수정으로 번지지 않는다.

**부분 결과를 흘려야 한다.** 문장 전체를 합성한 뒤 돌려주는 어댑터는 첫 소리가
문장 길이만큼 늦어진다. 사람은 0.5초 안에 반응이 없으면 다시 말하기 시작하므로,
긴 문장 하나가 대화를 통째로 무너뜨린다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from vai_contracts.speech import VoiceProfile


class BaseTTSAdapter(ABC):
    """음성 합성 엔진 어댑터."""

    name: str
    native_sample_rate: int
    """엔진이 내는 표본율. 이것과 요청 표본율이 다르면 호출부가 재표본화한다.
    엔진에게 8kHz로 직접 합성시키면 음질이 눈에 띄게 나빠진다."""

    @abstractmethod
    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        """모델 로드 및 GPU 메모리 할당."""

    @abstractmethod
    def synthesize_stream(self, text: str, voice: VoiceProfile) -> AsyncGenerator[bytes, None]:
        """PCM16 조각을 흘린다.

        조각 크기는 엔진이 정한다. 호출부는 받는 대로 즉시 다음 단계로
        넘겨야 하며, 모아 두었다 보내면 스트리밍의 의미가 없다.

        구현체는 ``async def`` + ``yield``로 쓴다. 여기서는 시그니처만 선언하고
        본문을 두지 않는다 — 본문에 ``yield``를 두면 이 추상 메서드 자체가
        제너레이터가 되어 타입이 어긋난다.
        """

    async def close(self) -> None:
        return None
