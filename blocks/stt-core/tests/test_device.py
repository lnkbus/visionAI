"""요청한 장치를 실제로 쓸 수 있는가.

GPU 관련 실패는 늦게, 그리고 나쁜 자리에서 드러난다. 컨테이너가 크래시 루프에
빠지고 로그에는 CUDA 초기화 오류만 남는데, 그걸 보고 "이 장비는 GPU 를 넘길 수
없다"까지 짚기는 어렵다.

특히 맥은 **카드가 꽂혀 있어도** 컨테이너가 GPU 를 못 쓴다 — Docker Desktop 은
리눅스 VM 안에서 돌고 GPU 통과 경로가 없다. 인텔 맥 + NVIDIA 조합에서 이걸
모르면 시연 자리에서 처음 알게 된다.

여기서 지키는 규칙 하나: **조용히 CPU 로 내려가지 않는다.** 그러면 "제안서엔
GPU 라고 썼는데 현장은 CPU"가 되고, 그 사실은 아무 데도 안 적힌다.
"""

from __future__ import annotations

import pytest

from vai_stt_core.adapters import faster_whisper as fw


@pytest.fixture
def no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw, "cuda_device_count", lambda: 0)


@pytest.fixture
def one_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw, "cuda_device_count", lambda: 1)


def test_cpu는_그대로(no_gpu: None) -> None:
    assert fw.resolve_device("cpu") == "cpu"


def test_auto는_없으면_cpu(no_gpu: None) -> None:
    assert fw.resolve_device("auto") == "cpu"


def test_auto는_있으면_gpu(one_gpu: None) -> None:
    assert fw.resolve_device("auto") == "cuda"


def test_cuda는_없으면_거부한다(no_gpu: None) -> None:
    """가장 중요한 규칙. 조용히 CPU 로 내려가면 견적과 현장이 갈라진다."""
    with pytest.raises(RuntimeError) as caught:
        fw.resolve_device("cuda")

    message = str(caught.value)
    assert "맥" in message, "맥에서 왜 안 되는지 말해 줘야 한다"
    assert "auto" in message and "cpu" in message, "무엇을 하라는지 말해 줘야 한다"


def test_cuda는_있으면_통과(one_gpu: None) -> None:
    assert fw.resolve_device("cuda") == "cuda"


def test_빈_값은_기본값으로_본다(one_gpu: None) -> None:
    assert fw.resolve_device("") == "cuda"


def test_대소문자와_공백을_흡수한다(no_gpu: None) -> None:
    assert fw.resolve_device("  AUTO  ") == "cpu"


def test_엔진이_없으면_장치도_없다(monkeypatch: pytest.MonkeyPatch) -> None:
    """엔진 없는 가벼운 이미지에서 조회만으로 죽으면 안 된다."""
    import builtins

    real_import = builtins.__import__

    def missing(name: str, *args: object, **kwargs: object) -> object:
        if name == "ctranslate2":
            raise ImportError("no engine")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", missing)

    assert fw.cuda_device_count() == 0


def test_모르는_값은_바로_거부한다(no_gpu: None) -> None:
    """그대로 넘기면 엔진이 훨씬 뒤에서 알아보기 어려운 오류를 낸다."""
    with pytest.raises(ValueError, match="auto·cpu·cuda"):
        fw.resolve_device("gpu")
