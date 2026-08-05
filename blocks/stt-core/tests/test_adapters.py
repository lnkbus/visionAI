"""STT 어댑터 계약 테스트.

"엔진 코드를 고치지 않고 교체 가능"이라는 사양서 §1의 약속은, 레지스트리로
바꿔 끼운 어댑터가 같은 계약을 지킬 때만 성립한다. 여기서 그 계약을 고정한다.
"""

from __future__ import annotations

import pytest

from vai_stt_core.adapters import create_stt
from vai_stt_core.adapters.base import BaseSTTAdapter
from vai_stt_core.adapters.fake import DEFAULT_SCRIPT, FakeSTTAdapter

PCM = b"\x10\x00" * 800  # 50ms


async def drain(adapter: BaseSTTAdapter, pcm: bytes = PCM) -> list[dict]:
    return [result async for result in adapter.transcribe_stream(pcm, 16000)]


def test_registry_creates_known_adapter() -> None:
    assert isinstance(create_stt("fake"), FakeSTTAdapter)


def test_registry_rejects_unknown_adapter_with_usable_message() -> None:
    with pytest.raises(ValueError, match="fake"):
        create_stt("nope")


def test_registry_does_not_import_gpu_engines_at_module_load() -> None:
    """GPU 없는 CI·개발 환경에서도 레지스트리 import가 성공해야 한다."""
    import sys

    assert "torch" not in sys.modules
    assert "faster_whisper" not in sys.modules


async def test_result_satisfies_the_declared_contract() -> None:
    adapter = create_stt("fake")
    await adapter.initialize("", {})

    results = await drain(adapter)

    assert results, "발화가 있으면 결과가 나와야 한다"
    result = results[0]
    assert set(result) >= {"text", "is_final", "confidence"}
    assert isinstance(result["text"], str) and result["text"]
    assert isinstance(result["is_final"], bool)
    assert 0.0 <= result["confidence"] <= 1.0


async def test_empty_audio_yields_nothing() -> None:
    adapter = create_stt("fake")
    await adapter.initialize("", {})

    assert await drain(adapter, b"") == []


async def test_script_can_be_injected_for_scenario_demos() -> None:
    adapter = FakeSTTAdapter()
    await adapter.initialize("", {"script": ["첫 문장", "둘째 문장"]})

    first = await drain(adapter)
    second = await drain(adapter)
    third = await drain(adapter)

    assert first[0]["text"] == "첫 문장"
    assert second[0]["text"] == "둘째 문장"
    assert third[0]["text"] == "첫 문장", "스크립트는 순환해야 한다"


async def test_default_script_follows_the_spec_example() -> None:
    """사양서 §4 예시 대화가 데모에서 그대로 재현되어야 한다."""
    assert "카드 대금 결제일 연기" in DEFAULT_SCRIPT[1]
