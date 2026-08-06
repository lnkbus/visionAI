"""모델이 없을 때 **무엇을 하라고** 말하는가.

폐쇄망 제품에서 모델은 이미지에 안 들어간다(크고, 고객사마다 고른다). 그래서
"모델이 아직 없는 상태"는 예외가 아니라 **설치 중 정상 경로**다.

그때 엔진에 그냥 넘기면 이렇게 죽는다:

    huggingface_hub.errors.LocalEntryNotFoundError: ...
    Connection error, and we cannot find the requested files in the disk cache

컨테이너는 크래시 루프에 빠지고, 로그에는 모델 허브 URL 이 찍힌다. 폐쇄망
담당자가 그 문구를 보고 할 수 있는 일이 없다.

그래서 **경로부터 본다.** 엔진을 import 하기도 전에, 없는 경로면 무엇을
받아야 하는지와 모델 없이 돌리는 방법까지 적어서 멈춘다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vai_spk_dia.embedding import create_embedder
from vai_stt_core.adapters.faster_whisper import FasterWhisperAdapter

MISSING = "/없는경로/모델"


def test_stt_는_받는_방법을_알려_준다() -> None:
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(FasterWhisperAdapter().initialize(MISSING, {"device": "cpu"}))

    message = str(caught.value)
    assert MISSING in message, "어느 경로가 없는지 말해야 한다"
    assert "fetch_models.sh" in message, "무엇을 실행하라는지 말해야 한다"
    assert "fake" in message, "모델 없이 돌리는 길도 알려 줘야 한다"


def test_stt_는_엔진이_없어도_같은_말을_한다() -> None:
    """엔진이 안 깔린 가벼운 이미지에서도 ImportError 가 아니라 이 안내가
    나와야 한다 — 경로 점검이 import 보다 앞이라는 뜻이다."""
    with pytest.raises(RuntimeError, match="STT 모델이 없다"):
        asyncio.run(FasterWhisperAdapter().initialize(MISSING, {}))


def test_화자분리_임베더도_같은_안내를_한다() -> None:
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(create_embedder("speechbrain").initialize(MISSING, {}))

    message = str(caught.value)
    assert "fetch_models.sh" in message
    assert "spectral" in message, "모델 없이 내려가는 길을 알려 줘야 한다"


def test_옆에_무엇이_있는지_말해_준다(tmp_path: Path) -> None:
    """없는 경로만 말하면 부족하다.

    모델을 제대로 받아 놓고 이름이 한 칸 어긋난 경우가 가장 흔하다 —
    compose 기본값은 `small` 인데 32GB 안내를 따라 `large-v3-turbo` 를 받아
    두는 식이다. 답은 바로 옆 디렉토리에 있는데, 안 적어 주면 그것을 찾느라
    로그와 문서 사이를 왕복하게 된다. 실제로 세 번 왕복했다.
    """
    (tmp_path / "faster-whisper-large-v3-turbo").mkdir()

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(FasterWhisperAdapter().initialize(str(tmp_path / "faster-whisper-small"), {}))

    message = str(caught.value)
    assert "faster-whisper-large-v3-turbo" in message, f"옆에 있는 것을 안 알려 준다: {message}"
    assert "VAI_STT_MODEL_PATH" in message, "고칠 방법을 안 알려 준다"


def test_마운트_자체가_없으면_그것부터_말한다(tmp_path: Path) -> None:
    """디렉토리를 아예 안 붙인 것과, 붙였는데 이름이 다른 것은 할 일이 다르다."""
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(FasterWhisperAdapter().initialize(str(tmp_path / "없다" / "모델"), {}))

    assert "VAI_MODEL_DIR" in str(caught.value)


def test_비어_있으면_비었다고_말한다(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="비어 있다"):
        asyncio.run(FasterWhisperAdapter().initialize(str(tmp_path / "faster-whisper-small"), {}))
