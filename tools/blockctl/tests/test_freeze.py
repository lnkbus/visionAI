"""형상 고정 테스트.

형상 관리에서 가장 위험한 상태는 **버전은 그대로인데 내용이 다른 것**이다.
같은 버전을 표방하는 두 개의 다른 물건이 존재하게 되고, 심사 제출본과 현장
설치본이 다르다는 사실을 아무도 증명하지 못한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from blockctl.catalog import load_catalog
from blockctl.freeze import (
    FreezeError,
    diff,
    enabled_blocks,
    freeze_package,
    render,
    source_hash,
)

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "deploy" / "charts" / "visionai"
PACKAGES = REPO / "deploy" / "release" / "packages"


@pytest.fixture(scope="module")
def blocks() -> list:
    return load_catalog(REPO / "blocks")


def test_only_enabled_blocks_are_frozen(blocks: list) -> None:
    """카탈로그 전체를 적으면 그건 제품 형상이 아니라 개발 저장소 목록이다."""
    result = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    ids = {c.component_id for c in result.components if c.kind == "block"}

    assert "UI-MEET" in ids and "SPK-DIA" in ids
    assert "AUD-RTP" not in ids, "회의록 패키지에 전화 인입은 들어가지 않는다"
    assert "TA-ASSIST" not in ids


def test_libraries_and_infra_are_included(blocks: list) -> None:
    """블록만 적으면 반입 목록이 아니라 목차가 된다."""
    result = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    kinds = {c.kind for c in result.components}

    assert kinds == {"block", "library", "infra"}
    assert any(c.component_id == "infra:redis" for c in result.components)


def test_hash_is_stable_across_runs(blocks: list) -> None:
    """같은 입력에 같은 해시가 아니면 대조 도구로 쓸 수 없다."""
    first = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    second = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)

    assert first.configuration_hash == second.configuration_hash
    assert render(first) == render(second)


def test_packages_differ(blocks: list) -> None:
    meeting = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    aicc = freeze_package("aicc", blocks, root=REPO, chart_dir=CHART)

    assert meeting.configuration_hash != aicc.configuration_hash


def test_unknown_package_fails(blocks: list) -> None:
    with pytest.raises(FreezeError, match="오버레이"):
        freeze_package("nope", blocks, root=REPO, chart_dir=CHART)


def test_source_hash_notices_content_change(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    before = source_hash(tmp_path)

    target.write_text("x = 2\n", encoding="utf-8")

    assert source_hash(tmp_path) != before


def test_source_hash_ignores_tests(tmp_path: Path) -> None:
    """테스트만 고쳤는데 형상이 바뀌었다고 하면 그 신호는 곧 무시된다."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    before = source_hash(tmp_path)

    (tmp_path / "tests" / "test_a.py").write_text("assert True\n", encoding="utf-8")

    assert source_hash(tmp_path) == before


def test_diff_names_the_silent_content_change(blocks: list) -> None:
    """버전은 그대로인데 내용이 바뀐 경우를 이름으로 짚어야 한다."""
    current = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    recorded = current.to_dict()
    recorded["configuration_hash"] = "다른해시"
    recorded["components"][0]["source_hash"] = "0000000000000000"

    problems = diff(current, recorded)

    assert any("내용 변경(버전 그대로)" in problem for problem in problems)


def test_diff_reports_removed_component(blocks: list) -> None:
    current = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    recorded = current.to_dict()
    recorded["configuration_hash"] = "다른해시"
    recorded["components"].append({"id": "GHOST", "version": "9.9.9"})

    assert any("빠짐: GHOST" in problem for problem in diff(current, recorded))


def test_identical_freeze_has_no_findings(blocks: list) -> None:
    current = freeze_package("meeting", blocks, root=REPO, chart_dir=CHART)
    assert diff(current, current.to_dict()) == []


def test_enabled_blocks_reads_the_overlay() -> None:
    names = enabled_blocks(CHART / "values-meeting.yaml")
    assert "ui-meet" in names and "aud-rtp" not in names


@pytest.mark.parametrize("package", ["meeting", "aicc", "voicebot", "avatar"])
def test_recorded_freeze_matches_the_repo(blocks: list, package: str) -> None:
    """레포에 기록된 형상 명세가 지금의 구성과 같은지 — CI 게이트.

    형상이 조용히 어긋나는 것을 막는다. 의도한 변경이면 `blockctl freeze
    <패키지> --write`로 갱신하고, 그 diff가 리뷰에 남는다.
    """
    recorded = yaml.safe_load((PACKAGES / f"{package}.yaml").read_text(encoding="utf-8"))
    current = freeze_package(package, blocks, root=REPO, chart_dir=CHART)

    problems = diff(current, recorded)

    assert not problems, "형상 명세 갱신 필요:\n" + "\n".join(problems)
