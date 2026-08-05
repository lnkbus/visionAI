"""결정적 STT 어댑터 — CI와 데모용.

GPU 없는 환경에서 파이프라인 전체(VAD → STT → 필터 → 팝업)를 통합 테스트하려면
반드시 필요하다. docs/06 §2-6의 "fake 어댑터 필수" 규칙을 만족한다.

기본 스크립트는 사양서 §4의 예시 대화를 따른다 — 데모에서 그대로 재현되도록.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from vai_stt_core.adapters.base import BaseSTTAdapter, SttResult

DEFAULT_SCRIPT: list[str] = [
    "안녕하세요, 상담원입니다. 무엇을 도와드릴까요?",
    "이번 달 카드 대금 결제일 연기가 가능한가요?",
    "제 주민등록번호는 900101-1234567 입니다.",
    "연체 전이라면 최대 5일까지 신청 가능합니다.",
]


class FakeSTTAdapter(BaseSTTAdapter):
    name = "fake"

    def __init__(self, script: list[str] | None = None) -> None:
        self._script = list(script or DEFAULT_SCRIPT)
        self._cursor = 0

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        script = config.get("script")
        if script:
            self._script = list(script)
        self._cursor = 0

    async def transcribe_stream(
        self, audio_chunk: bytes, sample_rate: int = 16000
    ) -> AsyncGenerator[SttResult, None]:
        if not audio_chunk:
            return
        text = self._script[self._cursor % len(self._script)]
        self._cursor += 1
        yield SttResult(
            text=text,
            is_final=True,
            confidence=0.95,
            speaker_id=None,
            language="ko",
        )

    def reset(self) -> None:
        """테스트에서 스크립트 위치를 되돌린다."""
        self._cursor = 0
