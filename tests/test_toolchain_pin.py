"""파이썬 버전이 한 곳에서만 정해지는가 — 형상 게이트.

인터프리터는 **납품 형상의 일부**다. 그런데 이 레포에서는 네 곳이 각자 답을
갖고 있었고, 서로 달랐다:

    선언된 하한       3.11  (requires-python)
    배포 이미지       3.12  (deploy/docker/Dockerfile)
    mypy 설정         3.11  (pyproject에 고정)
    CI가 실제로 쓴 것  3.12  (uv가 러너에서 잡은 것)

결과가 둘이었다. **납품되는 인터프리터(3.12)에서는 타입 검사도 테스트도 돈
적이 없었고**, mypy는 3.12용으로 설치된 numpy 스텁을 3.11 문법으로 읽다가
남의 파일에서 구문 오류를 냈다. 어느 로그에도 "버전이 갈렸다"는 말은 없었다.
로컬은 3.11이라 재현되지도 않았다.

정리 방향은 **3.12 단일화**다. 3.11 하한은 고객사 기존 런타임을 받으려고
열어 둔 것이었는데, 열어 둔 채 검증하지 않으면 그건 지원이 아니라 미확인
주장이다. 범위를 좁혀 하나를 확실히 하는 쪽을 골랐다.

그래서 여기서 보는 것은 하나다 — **버전을 말하는 모든 곳이 같은 값을
말하는가.** 기준은 ``.python-version`` 한 곳이고, 나머지는 전부 파생이다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / ".python-version"
ROOT_PYPROJECT = REPO / "pyproject.toml"
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile"


def pinned() -> str:
    """단일 기준. 다른 모든 선언은 이것과 맞아야 한다."""
    assert PIN.is_file(), ".python-version이 없다 — 인터프리터가 우연히 정해진다"
    return PIN.read_text(encoding="utf-8").strip()


def _root() -> str:
    return ROOT_PYPROJECT.read_text(encoding="utf-8")


def test_pin_is_a_plain_minor_version() -> None:
    assert re.fullmatch(r"3\.\d+", pinned()), f"'{pinned()}' — 3.12 형태여야 한다"


def test_running_interpreter_matches_the_pin() -> None:
    """지금 이 테스트를 돌리는 인터프리터가 곧 검증 대상이다.

    이 검사가 실패하면 "방금 통과한 테스트가 납품될 파이썬에서 통과한 것이
    아니다"라는 뜻이고, 그 결과는 근거로 쓸 수 없다.
    """
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert actual == pinned(), (
        f"실행 중인 파이썬 {actual} ≠ 고정값 {pinned()} — "
        "`uv sync --all-packages`로 환경을 다시 맞춘다"
    )


def test_docker_image_matches_the_pin() -> None:
    """배포 이미지가 곧 고객사에서 도는 인터프리터다.

    여기가 어긋나면 "우리 환경에서는 됐는데"가 성립한다 — 온프렘 납품에서
    그 말은 아무 도움이 되지 않는다.
    """
    found = re.search(r"^FROM python:(\d+\.\d+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert found, "Dockerfile에서 python 베이스 이미지를 못 찾았다"
    assert found.group(1) == pinned(), (
        f"배포 이미지 {found.group(1)} ≠ 검증 대상 {pinned()} — "
        "납품되는 인터프리터에서 아무것도 돌려 보지 않은 상태가 된다"
    )


def test_ruff_targets_the_version_we_ship() -> None:
    """타깃이 낮으면 신 문법이 잘못 고쳐지고, 높으면 안 되는 문법이 통과한다."""
    found = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', _root())
    assert found, "pyproject에 ruff target-version이 없다"
    assert f"{found.group(1)}.{found.group(2)}" == pinned()


def test_mypy_version_is_not_pinned() -> None:
    """mypy는 돌고 있는 인터프리터를 따라야 한다.

    고정하면 "설치된 스텁은 3.12용인데 문법은 3.11로 읽는" 상태가 되고,
    우리 코드가 아니라 서드파티 스텁에서 구문 오류가 난다. 버전을 두 곳에서
    정하는 순간 둘째가 언젠가 어긋난다.
    """
    mypy_section = _root().split("[tool.mypy]", 1)[-1]
    assert re.search(r"^\s*python_version\s*=", mypy_section, re.M) is None, (
        "mypy python_version이 고정돼 있다 — .python-version 한 곳만 쓴다"
    )


def test_ci_does_not_redeclare_the_version() -> None:
    """워크플로가 버전을 또 적으면 그것이 두 번째 기준이 된다.

    uv는 ``.python-version``을 읽는다. CI에 ``--python 3.x``를 박아 두면
    파일을 고쳐도 CI만 옛 버전으로 남고, 그 사실은 실패로 드러나지 않는다.
    """
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    hardcoded = re.findall(r"--python\s+([\d.]+)", workflow)
    assert not hardcoded, f"CI가 파이썬 버전을 따로 박아 두었다: {hardcoded}"


@pytest.mark.parametrize(
    "pyproject", sorted(REPO.glob("*/*/pyproject.toml")), ids=lambda p: p.parent.name
)
def test_workspace_members_declare_the_same_floor(pyproject: Path) -> None:
    """멤버가 제각각 하한을 말하면 블록 단위 납품에서 약속이 갈린다.

    검증하지 않은 지원 선언이 정확히 이렇게 생긴다 — 아무도 안 도는 버전이
    파일에만 적혀 있는 것.
    """
    found = re.search(r'requires-python\s*=\s*">=([\d.]+)"', pyproject.read_text(encoding="utf-8"))
    assert found, f"{pyproject}: requires-python이 없다"
    assert found.group(1) == pinned()


def test_root_declares_the_same_floor() -> None:
    found = re.search(r'requires-python\s*=\s*">=([\d.]+)"', _root())
    assert found, "루트 pyproject에 requires-python이 없다"
    assert found.group(1) == pinned()
