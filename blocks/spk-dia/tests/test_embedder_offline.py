"""반입한 가중치를 **실제로 쓰는가.**

맥 시연에서 SPK-DIA 가 이렇게 죽었다:

    huggingface_hub.file_download._hf_hub_download_to_cache_dir
    PermissionError: [Errno 13] Permission denied: '/home/visionai'

가중치는 `/models/spkrec-ecapa-voxceleb` 에 85MB 가 멀쩡히 있었다. 원인은
받아 온 `hyperparams.yaml` 안에 있었다:

    pretrained_path: speechbrain/spkrec-ecapa-voxceleb

**허브 저장소 이름이다.** speechbrain 은 바로 옆의 가중치를 무시하고 그리로
나간다. 반입한 85MB 가 통째로 안 쓰인다.

이게 특히 나쁜 이유는 **인터넷이 있으면 안 드러난다**는 것이다. 개발 장비에서는
조용히 다시 받아서 잘 도는 것처럼 보이고, 폐쇄망에 들어가서야 연결 실패로
죽는다. 우리가 본 것은 그 중간 상태였다 — 나가려다 캐시 자리를 못 만들어
`PermissionError` 가 났고, 그 경로는 우리 코드 어디에도 없는 이름이었다.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from vai_spk_dia.embedding import SPEECHBRAIN_FILES, SpeechBrainEmbedder, create_embedder

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"


def test_허브가_아니라_반입한_자리를_본다() -> None:
    """`overrides` 로 못 박지 않으면 yaml 이 적어 놓은 허브 주소가 이긴다."""
    source = inspect.getsource(SpeechBrainEmbedder.initialize)

    assert "pretrained_path" in source, (
        "hyperparams.yaml 의 pretrained_path 를 덮어쓰지 않는다 — "
        "반입한 가중치를 두고 허브로 나간다"
    )
    assert "savedir" in source, (
        "savedir 를 안 주면 읽기 전용으로 마운트된 모델 디렉토리에 쓰려 든다"
    )


def test_반입이_덜_됐으면_파일_이름을_말한다(tmp_path: Path) -> None:
    """디렉토리 존재만 보면 **빈 디렉토리를 통과시킨다.** 그 다음은 허브다."""
    half = tmp_path / "spkrec-ecapa-voxceleb"
    half.mkdir()
    (half / "hyperparams.yaml").write_text("x", "utf-8")

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(SpeechBrainEmbedder().initialize(str(half), {}))

    message = str(caught.value)
    assert "embedding_model.ckpt" in message, f"무엇이 없는지 말해야 한다: {message}"
    assert "fetch_models.sh" in message
    assert "spectral" in message, "모델 없이 내려가는 길도 알려 줘야 한다"


def test_경로가_아예_없으면_그것부터_말한다(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="디렉토리 자체가 없다"):
        asyncio.run(create_embedder("speechbrain").initialize(str(tmp_path / "없다"), {}))


def test_필요한_파일_목록이_비어_있지_않다() -> None:
    """목록이 비면 위 검사가 전부 조용히 통과한다."""
    assert "hyperparams.yaml" in SPEECHBRAIN_FILES
    assert any(name.endswith(".ckpt") for name in SPEECHBRAIN_FILES), "가중치를 안 본다"


# ── 이미지 ───────────────────────────────────────────────────────────────────


def test_이미지에_쓸_수_있는_집_디렉토리가_있다() -> None:
    """`useradd --system` 은 집 디렉토리를 안 만든다. 그러면 캐시 자리를 찾는
    라이브러리가 전부 `Permission denied: '/home/visionai'` 로 죽는데, 그
    경로는 우리 코드 어디에도 안 나온다 — 로그를 받은 사람이 무엇을 하라는
    말인지 알 수 없다."""
    text = DOCKERFILE.read_text("utf-8")

    assert "--create-home" in text or "mkdir -p /home/visionai" in text, (
        "비루트 사용자에게 쓸 수 있는 집 디렉토리가 없다"
    )
    assert "XDG_CACHE_HOME" in text, "캐시 자리를 정해 주지 않으면 라이브러리마다 다른 곳을 찾는다"


def test_이미지는_기동_중에_밖으로_나가지_않는다() -> None:
    """폐쇄망 제품의 전제다. 어댑터들은 local_files_only 로 열지만 그건 우리가
    부르는 자리에서만 참이고, 라이브러리가 자기 판단으로 허브를 부르면 그
    전제가 조용히 깨진다 — **인터넷이 있는 개발 장비에서는 안 드러난다.**"""
    text = DOCKERFILE.read_text("utf-8")

    assert "HF_HUB_OFFLINE=1" in text, (
        "허브 호출을 이미지에서 막지 않는다 — 나가려는 시도가 고객사 망에서야 드러난다"
    )
