"""TTS 어댑터 레지스트리."""

from __future__ import annotations

from collections.abc import Callable

from vai_tts_core.adapters.base import BaseTTSAdapter
from vai_tts_core.adapters.fake import FakeTTSAdapter


def _cosyvoice() -> BaseTTSAdapter:
    # 지연 import. 폐쇄망 PoC에는 신경망 엔진 의존성이 없는 경우가 많고,
    # 모듈 최상단에서 import 하면 fake 어댑터만 쓰는 구성도 기동하지 못한다.
    from vai_tts_core.adapters.neural import CosyVoiceAdapter

    return CosyVoiceAdapter()


def _kokoro() -> BaseTTSAdapter:
    from vai_tts_core.adapters.neural import KokoroAdapter

    return KokoroAdapter()


_REGISTRY: dict[str, Callable[[], BaseTTSAdapter]] = {
    "fake": FakeTTSAdapter,
    "cosyvoice": _cosyvoice,
    "kokoro": _kokoro,
}


def create_adapter(name: str) -> BaseTTSAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 TTS 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["BaseTTSAdapter", "available", "create_adapter"]
