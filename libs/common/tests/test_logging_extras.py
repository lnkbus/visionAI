"""붙인 진단 정보가 로그에 실제로 나오는가.

블록들은 `extra={...}` 로 판단 근거를 붙인다 — 어느 장치를 골랐는지, 왜
내려갔는지, 어느 세션인지. JSON 포맷은 그걸 최상위 필드로 올렸지만 **사람이
읽는 포맷은 통째로 버렸다.** 맥에서 기동 로그를 볼 때 이렇게 나왔다:

    INFO [STT-CORE] ...faster_whisper — STT 장치 자동 선택

GPU 로 갔는지 CPU 로 갔는지 보려고 켠 로그인데 그 답이 없다. 문서에는
"어느 쪽으로 갔는지 로그에 남는다"라고 적어 두었으니 더 나쁘다.

없어진 자리가 하필 **기본값이자 사람이 보는 쪽**이었다. JSON 을 켜고 보는
사람은 멀쩡했으니 개발 중에는 드러나지 않는다.
"""

from __future__ import annotations

import json
import logging

import pytest

from vai_common.logging import JsonFormatter, TextFormatter


def _record(message: str = "STT 장치 자동 선택", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("vai", logging.INFO, "f.py", 1, message, None, None)
    record.__dict__.update(extra)
    return record


def test_사람이_읽는_형식에도_필드가_남는다() -> None:
    line = TextFormatter("STT-CORE").format(_record(device="cpu", cuda_devices=0))

    assert "device=cpu" in line, "무엇을 골랐는지가 로그에 없다"
    assert "cuda_devices=0" in line


def test_적은_순서를_지킨다() -> None:
    """부른 쪽이 중요한 것을 먼저 적었을 테니 그 뜻을 지킨다."""
    line = TextFormatter("STT-CORE").format(_record(device="cpu", cuda_devices=0))

    assert line.index("device=cpu") < line.index("cuda_devices=0")


def test_공백이_섞인_값은_경계를_보여_준다() -> None:
    line = TextFormatter("CORE-SEC").format(
        _record("권한을 못 조였다", reason="Operation not permitted")
    )

    assert "reason='Operation not permitted'" in line, f"값의 끝이 안 보인다: {line}"


def test_붙인_게_없으면_예전_그대로다() -> None:
    line = TextFormatter("STT-CORE").format(_record())

    assert line.endswith("STT 장치 자동 선택"), f"꼬리표가 없는데 뭔가 붙었다: {line}"
    assert "·" not in line


def test_예외는_필드_뒤에_그대로_붙는다() -> None:
    try:
        raise RuntimeError("터졌다")
    except RuntimeError:
        import sys

        record = _record("CUDA 조회 실패", requested="cuda")
        record.exc_info = sys.exc_info()

    line = TextFormatter("STT-CORE").format(record)

    assert "requested=cuda" in line
    assert "RuntimeError: 터졌다" in line, "트레이스백이 사라졌다"
    assert line.index("requested=cuda") < line.index("Traceback"), (
        "꼬리표가 트레이스백 뒤로 밀렸다 — 첫 줄만 보는 사람에게는 안 보인다"
    )


def test_핸들러가_둘이어도_한_번만_붙는다() -> None:
    """레코드를 그 자리에서 고치면 다음 핸들러가 이미 꼬리표가 붙은 메시지를
    다시 받는다. 파일과 표준출력에 함께 쓰는 구성에서 바로 드러난다."""
    record = _record(device="cpu")
    formatter = TextFormatter("STT-CORE")

    first = formatter.format(record)
    second = formatter.format(record)

    assert first == second, f"두 번째가 다르다 — 레코드를 건드렸다\n{first}\n{second}"
    assert second.count("device=cpu") == 1


@pytest.mark.parametrize("as_json", [True, False])
def test_두_포맷이_같은_것을_담는다(as_json: bool) -> None:
    """포맷은 보기 방식이지 담는 내용이 아니다. 한쪽에만 있으면 '개발에서는
    보였는데 운영에서는 안 보인다'가 된다."""
    record = _record(device="cpu", session_id="s-1")

    if as_json:
        line = JsonFormatter("STT-CORE").format(record)
        assert json.loads(line)["device"] == "cpu"
        assert json.loads(line)["session_id"] == "s-1"
    else:
        line = TextFormatter("STT-CORE").format(record)
        assert "device=cpu" in line
        assert "session_id=s-1" in line
