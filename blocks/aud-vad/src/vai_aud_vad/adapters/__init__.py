"""VAD 어댑터 레지스트리.

새 엔진 추가 = 여기에 한 줄. 워커 코드는 어떤 엔진이 붙었는지 모른다.
"""

from __future__ import annotations

from collections.abc import Callable

from vai_aud_vad.adapters.base import BaseVadAdapter
from vai_aud_vad.adapters.energy import EnergyVadAdapter

_REGISTRY: dict[str, Callable[[], BaseVadAdapter]] = {
    "energy": EnergyVadAdapter,
}


def _load_silero() -> BaseVadAdapter:
    # torch는 선택적 extra다. 미설치 환경에서 import 비용을 물지 않도록 지연 로드한다.
    from vai_aud_vad.adapters.silero import SileroVadAdapter

    return SileroVadAdapter()


_REGISTRY["silero"] = _load_silero


def create_vad(name: str) -> BaseVadAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 VAD 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None


__all__ = ["BaseVadAdapter", "EnergyVadAdapter", "create_vad"]
