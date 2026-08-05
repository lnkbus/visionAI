"""설치 마법사 — 화면이 설치 스크립트와 어긋나지 않는가.

고객사 서버에서 도는 유일한 GUI다. 여기서 잘못되면 **설치가 실패한 줄
모르거나, 되지 않은 것을 됐다고 본다.** 둘 다 현장에서만 드러난다.

세 가지를 본다:

1. 마법사가 표준 라이브러리만 쓰는가 — 폐쇄망 서버에는 pip이 없다
2. 진행 표시 규약이 스크립트와 화면 양쪽에서 같은가
3. 번들을 읽고 설치 명령을 만드는 로직이 맞는가

마법사는 워크스페이스 패키지가 아니라 번들에 들어가는 파일이라 import 경로가
없다. 그래서 파일에서 직접 읽어 들인다 — 그 사실 자체도 검사한다.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "deploy" / "airgap" / "installer"
SERVER = INSTALLER / "server.py"
PAGE = INSTALLER / "index.html"
SCRIPT = REPO / "deploy" / "airgap" / "install.sh"


@cache
def load_server() -> Any:
    """마법사를 파일에서 직접 읽어 들인다 — 번들에 들어가는 파일이라 import 경로가 없다.

    **지연 로딩이다.** 모듈 수준에서 실행하면 마법사가 서드파티를 import하는
    순간 이 파일 전체가 수집 단계에서 죽고, 정작 그 사실을 말해 줄 검사는
    돌지도 못한다. 게다가 그 서드파티가 개발 머신에 깔려 있으면 통과해
    버린다 — 고객사 서버에는 없는데.
    """
    spec = importlib.util.spec_from_file_location("vai_installer_server", SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. 폐쇄망 제약 ──────────────────────────────────────────────────────────

STDLIB_ONLY_MSG = (
    "설치 마법사가 서드파티 모듈을 쓴다: {names} — 폐쇄망 서버에는 pip이 없다. "
    "마법사가 의존성을 요구하면 그 의존성부터 반입해야 하고, 마법사가 풀려던 "
    "문제를 마법사가 만든다."
)


def test_wizard_imports_stdlib_only() -> None:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    outside = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert not outside, STDLIB_ONLY_MSG.format(names=outside)


def test_wizard_binds_loopback_only() -> None:
    """마법사는 인증 없이 docker를 조작한다. 외부에 열면 원격 코드 실행 통로다."""
    assert load_server().BIND_HOST == "127.0.0.1"
    source = SERVER.read_text(encoding="utf-8")
    assert '"0.0.0.0"' not in source, "0.0.0.0 바인딩이 남아 있다"


def test_page_has_no_external_assets() -> None:
    """폐쇄망에서는 CDN이 없다. 외부를 참조하면 화면이 깨진 채로 뜬다."""
    html = PAGE.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
    assert not external, f"외부 자산을 참조한다: {external}"


# ── 2. 진행 표시 규약 ───────────────────────────────────────────────────────


def test_script_emits_every_kind_the_server_parses() -> None:
    """규약이 한쪽에만 있으면 화면이 조용히 비거나 멈춘 것처럼 보인다."""
    script = SCRIPT.read_text(encoding="utf-8")
    emitted = set(re.findall(r"mark (STEP|OK|FAIL|DONE|ABORT)\b", script))
    parsed = set(load_server().MARK.pattern.split("(")[1].split(")")[0].split("|"))
    assert emitted == parsed, f"스크립트가 내보내는 표시 {emitted} ≠ 서버가 읽는 표시 {parsed}"


def test_markers_are_silent_without_the_flag() -> None:
    """사람이 보는 출력에 @@ 표시가 섞이면 설치 로그가 읽기 나빠진다."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "VAI_PROGRESS:-0" in script and '"1"' in script, "표시가 플래그로 가려지지 않는다"


def test_page_knows_the_same_step_count() -> None:
    """단계 수가 갈리면 진행률이 100%를 넘거나 못 미친다."""
    script_total = set(
        re.findall(r"mark STEP \"\$1\|(\d+)\|", script_text := SCRIPT.read_text("utf-8"))
    )
    assert script_total == {"5"}, f"스크립트 단계 수가 하나가 아니다: {script_total}"
    steps = re.search(r"const STEPS = \[(.*?)\];", PAGE.read_text("utf-8"), re.S)
    assert steps, "화면에 STEPS 목록이 없다"
    assert len(re.findall(r'"[^"]+"', steps.group(1))) == 5
    assert script_text.count("step ") >= 5


@pytest.mark.parametrize("index", range(1, 6))
def test_every_step_is_announced(index: int) -> None:
    assert re.search(rf"^step {index} ", SCRIPT.read_text(encoding="utf-8"), re.M), (
        f"{index}단계가 진행 표시를 내보내지 않는다 — 화면이 그 구간에서 멈춘 것처럼 보인다"
    )


# ── 3. 번들 읽기와 명령 구성 ────────────────────────────────────────────────


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "blocks": ["TA-ASSIST", "CORE-BUS", "STT-CORE"],
                "added_by_dependency": ["CORE-BUS", "STT-CORE"],
                "infra": ["redis"],
                "images": ["visionai/ta-assist:0.2.0", "redis:7-alpine"],
                "warnings": ["생산자가 없다"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "VERSION").write_text("0.9.0\n", encoding="utf-8")
    (tmp_path / "images.tar").write_bytes(b"x" * (5 * 1048576))
    return tmp_path


def test_read_bundle_marks_dependencies_locked(bundle: Path) -> None:
    """의존으로 끌려온 블록을 끌 수 있게 두면 켠 블록이 못 돈다."""
    info = load_server().read_bundle(bundle)
    locked = {b["id"] for b in info["blocks"] if b["required"]}
    assert locked == {"CORE-BUS", "STT-CORE"}
    assert info["version"] == "0.9.0"
    assert info["image_count"] == 2
    assert info["images_mb"] == 5
    assert info["warnings"] == ["생산자가 없다"]


def test_read_bundle_names_every_block_in_the_catalog() -> None:
    """이름이 빠진 블록은 화면에 ID만 나온다 — 고객사가 무엇인지 알 수 없다."""
    catalog = {p.parent.name.upper() for p in REPO.glob("blocks/*/block.yaml")}
    missing = sorted(catalog - set(load_server().BLOCK_NAMES))
    assert not missing, f"마법사에 이름이 없는 블록: {missing}"


def test_read_bundle_rejects_a_broken_bundle(tmp_path: Path) -> None:
    """plan.json 없이 설치를 시작하면 무엇이 깔리는지 모른 채 깔린다."""
    with pytest.raises(load_server().BundleError, match=r"plan\.json"):
        load_server().read_bundle(tmp_path)


def test_install_argv_passes_the_selection(bundle: Path) -> None:
    argv = load_server().install_argv(
        bundle, dry_run=False, blocks=["TA-ASSIST", "CORE-BUS"], data_dir="/srv/vai"
    )
    assert argv[:2] == ["bash", str(bundle / "install.sh")]
    assert "--blocks" in argv and argv[argv.index("--blocks") + 1] == "TA-ASSIST CORE-BUS"
    assert argv[argv.index("--data-dir") + 1] == "/srv/vai"
    assert "--dry-run" not in argv


def test_install_argv_without_selection_starts_everything(bundle: Path) -> None:
    """선택이 없으면 지금까지처럼 전부 띄운다 — 기존 절차를 깨지 않는다."""
    argv = load_server().install_argv(bundle, dry_run=True, blocks=[], data_dir="")
    assert "--blocks" not in argv
    assert "--dry-run" in argv


def test_script_accepts_the_options_the_server_sends(bundle: Path) -> None:
    """서버가 보내는 옵션을 스크립트가 모르면 '알 수 없는 옵션'으로 죽는다."""
    script = SCRIPT.read_text(encoding="utf-8")
    argv = load_server().install_argv(bundle, dry_run=True, blocks=["A"], data_dir="/x")
    for option in (arg for arg in argv if arg.startswith("--")):
        assert f"{option})" in script, f"install.sh가 {option} 을 모른다"
