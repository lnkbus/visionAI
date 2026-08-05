"""STT 모델 추상화 레이어 — 사양서 §3 모듈 1.

사양서에 정의된 ``BaseSTTAdapter`` 인터페이스를 그대로 구현하되, 반환 dict의
키 집합을 :class:`SttResult` TypedDict로 고정해 오타가 런타임까지 가지 않게 했다.
와이어 계약(dict 형태)은 사양서와 동일하다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any, NotRequired, TypedDict


class SttResult(TypedDict):
    """``transcribe_stream``이 내보내는 한 건의 인식 결과."""

    text: str
    is_final: bool
    confidence: float
    speaker_id: NotRequired[str | None]
    language: NotRequired[str]


class BaseSTTAdapter(ABC):
    """음성 인식 엔진 어댑터.

    구현체는 **프로세스당 하나**만 만들어 GPU 메모리를 공유한다. 세션별 상태가
    필요하면 어댑터 내부에서 세션 키로 관리한다(VAD와 달리 Whisper 계열은
    구간 단위 무상태 추론이라 대부분 불필요하다).
    """

    name: str

    @abstractmethod
    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        """모델 가중치 로드 및 GPU 메모리 할당."""

    @abstractmethod
    async def transcribe_stream(
        self, audio_chunk: bytes, sample_rate: int = 16000, *, hint: str = ""
    ) -> AsyncGenerator[SttResult, None]:
        """오디오 구간을 수신하여 실시간 STT 결과(Delta)를 생성한다.

        하나의 구간에서 여러 결과가 나올 수 있다(문장 단위 분할). 호출자는
        받는 대로 즉시 다음 단계로 흘려보내야 지연 예산을 지킬 수 있다.

        ``hint``는 디코딩 편향용 힌트다(커스텀 사전의 상품명·전문용어).
        사양서 원래 시그니처에 키워드 인자로만 덧붙여, 기존 어댑터는
        무시해도 동작이 깨지지 않는다.
        """
        raise NotImplementedError
        yield  # pragma: no cover - 시그니처를 async generator로 고정하기 위한 표식

    async def close(self) -> None:
        """GPU 메모리 해제."""
        return None
