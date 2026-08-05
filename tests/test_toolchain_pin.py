"""지원한다고 적은 파이썬 버전을 실제로 검증하는가 — 형상 게이트.

인터프리터는 **납품 형상의 일부**다. 그런데 이 레포에서는 네 곳이 각자 답을
갖고 있었고, 서로 달랐다:

    선언된 하한      3.11  (requires-python — 고객사 기존 런타임 수용)
    배포 이미지      3.12  (deploy/docker/Dockerfile)
    mypy 설정        3.11  (pyproject에 고정)
    CI가 실제로 쓴 것 3.12  (uv가 러너에서 잡은 것)

결과는 둘이었다. **납품되는 인터프리터(3.12)에서는 타입 검사도 테스트도 돈
적이 없었고**, mypy는 3.12용으로 설치된 numpy 스텁을 3.11 문법으로 읽다가
남의 파일에서 구문 오류를 냈다. 어느 로그에도 "버전이 갈렸다"는 말은 없었다.

고칠 방향이 둘이었다. 하한을 3.12로 올려 선언을 좁히거나, 하한을 지키고
양 끝을 다 검증하거나. **하한 3.11은 docs/06에 이유가 적힌 사업 판단**이라
(고객사 기존 런타임에 얹을 수 있게) 뒤엎지 않고 검증하는 쪽을 골랐다.

그래서 여기서 보는 것은 "어느 한 버전으로 통일됐는가"가 아니다.
**적어 놓은 범위와 실제로 도는 범위가 같은가**이다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ROOT_PYPROJECT = REPO / "pyproject.toml"
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _version(text: str) -> tuple[int, int]:
    major, minor = text.split(".")[:2]
    return int(major), int(minor)


def floor_version() -> tuple[int, int]:
    """지원 하한 — ``requires-python``이 고객사에 하는 약속이다."""
    found = re.search(r'requires-python\s*=\s*">=([\d.]+)"', ROOT_PYPROJECT.read_text("utf-8"))
    assert found, "루트 pyproject에 requires-python이 없다"
    return _version(found.group(1))


def ship_version() -> tuple[int, int]:
    """배포 이미지 — 고객사에서 실제로 도는 인터프리터다."""
    found = re.search(r"^FROM python:(\d+\.\d+)", DOCKERFILE.read_text("utf-8"), re.M)
    assert found, "Dockerfile에서 python 베이스 이미지를 못 찾았다"
    return _version(found.group(1))


def ci_versions() -> list[tuple[int, int]]:
    workflow = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    matrix = workflow["jobs"]["quality"]["strategy"]["matrix"]["python"]
    return [_version(str(v)) for v in matrix]


def _show(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


# ── 범위의 양 끝이 실제로 돌아야 한다 ───────────────────────────────────────


def test_ci_covers_the_declared_floor() -> None:
    """하한에서 안 돌리면 ``requires-python``은 검증되지 않은 약속이다.

    블록 단위로 떼어 파는 제품이라 이 선언은 문서가 아니라 납품 약속이 된다.
    """
    assert floor_version() in ci_versions(), (
        f"하한 {_show(floor_version())}을 CI가 돌리지 않는다 — "
        f"현재 매트릭스 {[_show(v) for v in ci_versions()]}"
    )


def test_ci_covers_the_shipped_image() -> None:
    """배포 이미지에서 안 돌리면 '우리 환경에서는 됐는데'가 성립한다.

    온프렘 납품에서 그 말은 아무 도움이 되지 않는다.
    """
    assert ship_version() in ci_versions(), (
        f"배포 이미지 {_show(ship_version())}을 CI가 돌리지 않는다 — "
        f"현재 매트릭스 {[_show(v) for v in ci_versions()]}"
    )


def test_shipped_image_is_within_the_declared_range() -> None:
    """이미지가 하한보다 낮으면 선언이 거짓이 된다."""
    assert ship_version() >= floor_version(), (
        f"배포 이미지 {_show(ship_version())} < 선언 하한 {_show(floor_version())}"
    )


@pytest.mark.parametrize("version", ci_versions(), ids=_show)
def test_ci_matrix_stays_within_the_declared_range(version: tuple[int, int]) -> None:
    """지원한다고 적지 않은 버전을 CI가 돌면 매트릭스가 근거를 잃는다."""
    assert version >= floor_version()


# ── 도구 설정이 범위와 맞아야 한다 ──────────────────────────────────────────


def test_ruff_targets_the_floor_not_the_ceiling() -> None:
    """린터는 **하한** 문법으로 봐야 한다.

    상한에 맞추면 3.12 전용 문법이 조용히 들어오고, 3.11 고객사에서 기동할
    때에야 구문 오류로 드러난다 — 그때는 현장이다.
    """
    found = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', ROOT_PYPROJECT.read_text("utf-8"))
    assert found, "pyproject에 ruff target-version이 없다"
    assert (int(found.group(1)), int(found.group(2))) == floor_version()


def test_mypy_version_is_not_pinned() -> None:
    """mypy는 돌고 있는 인터프리터를 따라야 한다.

    고정하면 "설치된 스텁은 3.12용인데 문법은 3.11로 읽는" 상태가 되고,
    우리 코드가 아니라 서드파티 스텁에서 구문 오류가 난다. 실제로 CI를
    세운 것이 이것이고, 로컬(3.11)에서는 재현되지 않았다.
    """
    mypy_section = ROOT_PYPROJECT.read_text("utf-8").split("[tool.mypy]", 1)[-1]
    setting = re.search(r"^\s*python_version\s*=", mypy_section, re.M)
    assert setting is None, (
        "mypy python_version이 고정돼 있다 — 매트릭스의 한쪽에서 반드시 어긋난다"
    )


def test_running_interpreter_is_a_supported_one() -> None:
    """지금 이 테스트를 돌리는 인터프리터가 곧 검증 대상이다.

    이 검사가 실패하면 "방금 통과한 테스트가 지원 범위 밖에서 통과한 것"이라는
    뜻이고, 그 결과는 근거로 쓸 수 없다.
    """
    actual = (sys.version_info.major, sys.version_info.minor)
    assert actual in ci_versions(), (
        f"실행 중인 파이썬 {_show(actual)}이 CI 매트릭스에 없다 — "
        f"{[_show(v) for v in ci_versions()]}에서 돌려야 결과가 근거가 된다"
    )


@pytest.mark.parametrize(
    "pyproject", sorted(REPO.glob("*/*/pyproject.toml")), ids=lambda p: p.parent.name
)
def test_workspace_members_declare_the_same_floor(pyproject: Path) -> None:
    """멤버가 제각각 하한을 말하면 블록 단위 납품에서 약속이 갈린다."""
    found = re.search(r'requires-python\s*=\s*">=([\d.]+)"', pyproject.read_text("utf-8"))
    assert found, f"{pyproject}: requires-python이 없다"
    assert _version(found.group(1)) == floor_version()
