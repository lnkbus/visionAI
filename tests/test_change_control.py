"""변경 통제 목록이 실물을 가리키는가 — 게이트의 게이트.

`.github/change-control-paths.txt`는 "이 경로는 PR을 거쳐야 한다"는 목록이다.
목록이 유령을 가리키면 게이트는 통과만 하고 아무것도 막지 않는다 — 그리고
그 사실은 막았어야 할 변경이 그냥 들어간 뒤에야 드러난다. 감사에서 반복해서
본 모양이라(정의만 하고 등록하지 않음, 구현만 하고 배선하지 않음) 여기서도
같은 자리에 검사를 둔다.

목록을 좁게 유지하는 것도 함께 본다. 모든 경로에 PR을 요구하면 규칙이 형식이
되고, 형식이 된 규칙은 정작 중요한 변경에서도 지켜지지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
PATHS_FILE = REPO / ".github" / "change-control-paths.txt"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def patterns() -> list[str]:
    lines = PATHS_FILE.read_text(encoding="utf-8").splitlines()
    return [
        stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def test_list_is_not_empty() -> None:
    assert patterns(), "통제 목록이 비었다 — 게이트가 아무것도 막지 않는다"


@pytest.mark.parametrize("pattern", patterns())
def test_every_pattern_matches_something(pattern: str) -> None:
    """유령 항목이 남으면 다음 사람이 그것을 근거로 삼는다.

    디렉토리는 존재만 확인하고, 글로브는 실제로 걸리는 파일이 있는지 본다.
    """
    if pattern.endswith("/"):
        assert (REPO / pattern).is_dir(), f"{pattern}: 디렉토리가 없다"
        return
    if any(ch in pattern for ch in "*?["):
        assert list(REPO.glob(pattern)), f"{pattern}: 걸리는 파일이 없다"
        return
    assert (REPO / pattern).exists(), f"{pattern}: 파일이 없다"


def test_list_stays_narrow() -> None:
    """소스 전체를 통제 대상으로 삼으면 혼합 정책이 아니라 전면 PR 정책이 된다.

    그러면 일상 수정마다 PR을 만들게 되고, 곧 아무도 diff를 읽지 않는다.
    """
    broad = [p for p in patterns() if p in {"blocks/", "libs/", "tools/", ".", "src/"}]
    assert not broad, (
        f"통제 목록이 너무 넓다: {broad} — 전부 PR이면 규칙이 형식이 된다. "
        "일상 변경은 main 직접 커밋으로 가는 것이 이 정책의 전제다"
    )


def test_the_gate_itself_is_controlled() -> None:
    """검사를 우회하는 변경이 검사 없이 들어가면 게이트가 무의미하다."""
    declared = patterns()
    assert any(p.startswith(".github/workflows") for p in declared), (
        "워크플로가 통제 대상이 아니다 — 게이트를 끄는 커밋이 게이트를 안 거친다"
    )
    assert ".github/change-control-paths.txt" in declared, (
        "통제 목록 자신이 통제 대상이 아니다 — 목록을 비우는 커밋이 그냥 들어간다"
    )


def test_workflow_reads_this_file() -> None:
    """워크플로가 목록을 하드코딩하면 이 파일은 장식이 된다."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"].get("change-control")
    assert job is not None, "ci.yml에 change-control 잡이 없다"

    script = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert ".github/change-control-paths.txt" in script, (
        "변경 통제 잡이 목록 파일을 읽지 않는다 — 두 곳에서 정하면 둘째가 어긋난다"
    )
    # 병합 커밋은 통과시켜야 한다. 그러지 않으면 PR로 올려도 막힌다.
    assert "rev-list --parents" in script, "병합 커밋 판별이 없다 — PR 경유도 막힌다"
