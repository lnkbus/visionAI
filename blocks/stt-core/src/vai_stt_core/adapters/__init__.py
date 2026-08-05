"""STT 어댑터 레지스트리 — 엔진 추가는 여기 한 줄로 끝난다."""

from __future__ import annotations

from collections.abc import Callable

from vai_stt_core.adapters.base import BaseSTTAdapter, SttResult
from vai_stt_core.adapters.fake import FakeSTTAdapter


def _load_faster_whisper() -> BaseSTTAdapter:
    from vai_stt_core.adapters.faster_whisper import FasterWhisperAdapter

    return FasterWhisperAdapter()


_REGISTRY: dict[str, Callable[[], BaseSTTAdapter]] = {
    "fake": FakeSTTAdapter,
    "faster_whisper": _load_faster_whisper,
}


def create_stt(name: str) -> BaseSTTAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 STT 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None


__all__ = ["BaseSTTAdapter", "FakeSTTAdapter", "SttResult", "create_stt"]
