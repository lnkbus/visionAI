"""설치 스크립트가 GNU coreutils 없이도 도는가 — 이식성 게이트.

고객사 서버는 리눅스니 상관없다고 생각하기 쉽다. 문제는 **개발자와 영업이
맥에서 번들을 열어 본다**는 것이고, 그때 `stat: illegal option -- c` 를
만나면 번들이 깨진 줄 안다. 실제로는 도구가 다를 뿐이다.

여기서 세 가지를 본다:

1. 반입되는 스크립트가 GNU 전용 도구·플래그를 직접 쓰지 않는가
2. macOS 기본 bash(3.2)에 없는 문법을 쓰지 않는가
3. 이식 계층이 실제로 두 환경에서 같은 답을 내는가

3번은 GNU 도구를 감춘 PATH를 만들어 진짜로 돌려 본다. 문자열 검사만 하면
"안 쓴다"는 것만 알 뿐 "된다"는 것은 모른다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AIRGAP = REPO / "deploy" / "airgap"
PORTABLE = AIRGAP / "portable.sh"

# 번들에 들어가 고객사 서버에서 도는 스크립트 + 이들을 만드는 빌드 스크립트.
SHIPPED = [
    AIRGAP / "install.sh",
    AIRGAP / "selftest.sh",
    AIRGAP / "build_bundle.sh",
    REPO / "deploy" / "release" / "build_release.sh",
]

# 이유를 함께 적는다. 목록만 있으면 다음 사람이 "왜 안 되는지" 모른 채 우회한다.
GNU_ONLY = {
    r"\bmapfile\b": "bash 4+ 전용 — macOS 기본 bash는 3.2다. read_into 를 쓴다",
    r"\breadarray\b": "mapfile 의 다른 이름 — 같은 제약",
    r"\bstat\s+-c": "GNU stat 전용 (BSD는 -f) — file_size 를 쓴다",
    r"\bsha256sum\b": "리눅스에만 있다 (macOS는 shasum -a 256) — sha256_of/sha256_list 를 쓴다",
    r"\bsort\s+-z\b": "GNU sort 전용 — sorted_files 를 쓴다",
    r"\bdf\s+-\w*m\b": "df -m 은 POSIX가 아니다 — avail_kb 를 쓴다",
    r"\breadlink\s+-f\b": "GNU readlink 전용",
    r"\bsed\s+-i\s+(?!\.)": "GNU sed 는 -i 뒤 인자가 없고 BSD 는 필요하다",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*,,\}": "${var,,} 는 bash 4+ 전용 — tr 를 쓴다",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^\}": "${var^^} 는 bash 4+ 전용 — tr 를 쓴다",
    r"\bdeclare\s+-A\b": "연관배열은 bash 4+ 전용",
    r"\blocal\s+-n\b": "namerefs 는 bash 4.3+ 전용",
}


def _body(path: Path) -> str:
    """주석을 뺀 본문. 주석에는 대비표로 GNU 이름이 등장한다."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script", SHIPPED, ids=lambda p: p.name)
@pytest.mark.parametrize(("pattern", "why"), list(GNU_ONLY.items()), ids=lambda v: str(v)[:24])
def test_shipped_scripts_avoid_gnu_only_tools(script: Path, pattern: str, why: str) -> None:
    found = re.search(pattern, _body(script))
    assert not found, f"{script.name}: {found.group(0) if found else pattern} — {why}"


def test_portable_layer_is_the_only_place_that_knows_about_gnu() -> None:
    """차이를 한 곳에 가둬야 다음 차이가 생겼을 때 고칠 자리가 하나다."""
    body = _body(PORTABLE)
    assert "sha256sum" in body and "shasum" in body, "해시 도구 분기가 이식 계층에 없다"
    assert "stat -c" in body and "stat -f" in body, "stat 분기가 이식 계층에 없다"


@pytest.mark.parametrize("script", [*SHIPPED, PORTABLE], ids=lambda p: p.name)
def test_scripts_parse(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{script.name} 문법 오류:\n{result.stderr}"


def test_bundle_scripts_carry_the_portable_layer() -> None:
    """install.sh 가 source 하는 파일이 번들에 안 들어가면 현장에서만 죽는다."""
    builder = (AIRGAP / "build_bundle.sh").read_text(encoding="utf-8")
    assert "portable.sh" in builder, "build_bundle.sh 가 이식 계층을 번들에 담지 않는다"
    for script in (AIRGAP / "install.sh", AIRGAP / "selftest.sh"):
        assert "portable.sh" in script.read_text(encoding="utf-8"), (
            f"{script.name} 이 이식 계층을 읽지 않는다"
        )


# ── 실제로 돌려 본다 ────────────────────────────────────────────────────────

BSD_TOOLS = (
    "awk cat cut df find grep sed sort tr wc head printf env bash sh dirname basename mkdir rm cp"
)


@pytest.fixture
def bsd_path(tmp_path: Path) -> str:
    """GNU 전용 도구를 감춘 PATH.

    ``sha256sum``을 **넣지 않는다** — 깨진 스텁을 두면 "있는데 실패"가 되어
    실제 macOS(아예 없음)와 다른 상황을 시험하게 된다.
    """
    binary = tmp_path / "bin"
    binary.mkdir()
    for tool in BSD_TOOLS.split():
        found = shutil.which(tool)
        if found:
            (binary / tool).symlink_to(found)

    real_stat = shutil.which("stat")
    (binary / "stat").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in -c*)\n'
        '  echo "stat: illegal option -- c" >&2; exit 1;; esac; done\n'
        f'exec {real_stat} "$@"\n',
        encoding="utf-8",
    )
    (binary / "shasum").write_text(
        f'#!/bin/sh\n[ "$1" = "-a" ] && shift 2\nexec {shutil.which("sha256sum")} "$@"\n',
        encoding="utf-8",
    )
    for name in ("stat", "shasum"):
        (binary / name).chmod(0o755)
    return str(binary)


# install.sh 가 도는 셸 옵션. 이것을 빼면 재현되지 않는 결함이 있다 —
# 실제로 무결성 검사가 `set -euo pipefail` 아래에서만 항상 통과했고, 옵션
# 없이 돌던 시험은 그 사실을 통과시켰다. 시험은 호출자의 환경을 재현해야 한다.
SHELL_OPTS = "set -euo pipefail"


def _run(snippet: str, path: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f"{SHELL_OPTS}\n. {PORTABLE}\n{snippet}"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": path},
        cwd=REPO,
    )
    assert result.returncode == 0, f"실패({result.returncode}):\n{result.stderr}"
    return result.stdout.strip()


def test_hash_tool_falls_back_without_sha256sum(bsd_path: str) -> None:
    assert _run("sha_cmd", bsd_path) == "shasum -a 256"


def test_file_size_matches_in_both_environments(bsd_path: str) -> None:
    target = "Makefile"
    gnu = _run(f"file_size {target}", os.environ["PATH"])
    bsd = _run(f"file_size {target}", bsd_path)
    assert gnu == bsd == str((REPO / target).stat().st_size)


def test_checksums_match_in_both_environments(bsd_path: str) -> None:
    """로케일이 다르면 정렬이 달라져 같은 내용에서 다른 SHA256SUMS 가 나온다."""
    snippet = "sorted_files deploy/airgap | sha256_list"
    assert _run(snippet, os.environ["PATH"]) == _run(snippet, bsd_path)


@pytest.mark.parametrize("path_kind", ["gnu", "bsd"])
def test_verify_fails_on_a_tampered_file(path_kind: str, bsd_path: str, tmp_path: Path) -> None:
    """무결성 검사가 **실제로 실패하는지**.

    처음 구현은 `… | grep -v ': OK$' || true` 뒤에 PIPESTATUS[0]을 돌려줬는데,
    `|| true`의 `true`가 단순 명령이라 PIPESTATUS를 (0)으로 덮어썼다. 그래서
    체크섬을 깨뜨려도 "일치"가 나왔다 — **검사가 항상 통과하고 있었다.**
    깨진 검사는 없는 검사보다 나쁘다. 통과했다는 근거로 쓰이기 때문이다.
    """
    path = bsd_path if path_kind == "bsd" else os.environ["PATH"]
    (tmp_path / "a.txt").write_text("원본\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("그대로\n", encoding="utf-8")
    subprocess.run(
        ["bash", "-c", f"{SHELL_OPTS}; . {PORTABLE}; sorted_files . | sha256_list > SHA256SUMS"],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PATH": path},
    )
    (tmp_path / "a.txt").write_text("바꿔치기\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'{SHELL_OPTS}; . {PORTABLE}\nif m="$(sha256_verify SHA256SUMS)"; '
            f'then echo "$m"; exit 0; else echo "$m"; exit 1; fi',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": path},
    )
    assert result.returncode != 0, "체크섬이 깨졌는데 통과했다 — 검사가 아무것도 막지 않는다"
    assert "a.txt" in result.stdout, "어느 파일이 깨졌는지 알려 주지 않는다"
    assert "b.txt" not in result.stdout, "멀쩡한 파일까지 불일치로 보고한다"


@pytest.mark.parametrize("path_kind", ["gnu", "bsd"])
def test_verify_passes_on_an_intact_bundle(path_kind: str, bsd_path: str, tmp_path: Path) -> None:
    """반대 방향도 본다 — 항상 실패하는 검사도 곧 무시된다."""
    path = bsd_path if path_kind == "bsd" else os.environ["PATH"]
    (tmp_path / "a.txt").write_text("원본\n", encoding="utf-8")
    subprocess.run(
        ["bash", "-c", f"{SHELL_OPTS}; . {PORTABLE}; sorted_files . | sha256_list > SHA256SUMS"],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PATH": path},
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'{SHELL_OPTS}; . {PORTABLE}\nif m="$(sha256_verify SHA256SUMS)"; '
            f'then echo "$m"; exit 0; else echo "$m"; exit 1; fi',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": path},
    )
    assert result.returncode == 0, f"온전한 번들이 실패했다:\n{result.stdout}{result.stderr}"


def test_read_into_replaces_mapfile(bsd_path: str) -> None:
    out = _run('read_into XS < <(printf "a\\nb\\nc\\n"); echo "${#XS[@]}:${XS[1]}"', bsd_path)
    assert out == "3:b"


# ── 아키텍처 ────────────────────────────────────────────────────────────────
#
# arm64 이미지는 개발 편의가 아니라 **K3s 실기동 리허설**을 위한 것이다.
# 리허설에는 GPU가 필요 없어 Apple Silicon 노트북이면 되는데, amd64 이미지만
# 있으면 QEMU 에뮬레이션으로 돌아 21개 블록이 전부 느려진다.


def test_host_arch_speaks_docker_notation() -> None:
    """uname -m 표기가 제각각이다. 번들의 ARCH 파일과 대조하려면 하나로 맞춰야 한다."""
    assert _run("host_arch", os.environ["PATH"]) in {"amd64", "arm64"}


def test_bundle_builder_defaults_to_amd64() -> None:
    """납품 기본값이 arm64로 넘어가면 고객사 서버(x86)에서 안 돈다.

    그 실패는 반입을 마친 뒤 기동 단계에서야 드러난다 — 되돌리려면
    반출입 승인을 다시 밟아야 한다.
    """
    builder = (AIRGAP / "build_bundle.sh").read_text(encoding="utf-8")
    found = re.search(r'^ARCH="([^"]+)"', builder, re.M)
    assert found, "build_bundle.sh 에 기본 아키텍처가 없다"
    assert found.group(1) == "amd64"


def test_bundle_builder_rejects_an_unknown_arch() -> None:
    """오타가 그대로 --platform 으로 넘어가면 docker 가 이해할 수 없는 오류를 낸다."""
    builder = (AIRGAP / "build_bundle.sh").read_text(encoding="utf-8")
    assert "amd64|arm64)" in builder, "허용 아키텍처 검사가 없다"


def test_installer_compares_the_bundle_arch() -> None:
    """아키텍처가 다른 번들은 'exec format error' 로 죽는다 — 이미지를 다 푼 뒤에."""
    script = (AIRGAP / "install.sh").read_text(encoding="utf-8")
    assert "host_arch" in script and "ARCH" in script, "설치기가 아키텍처를 대조하지 않는다"


@pytest.mark.parametrize(
    ("bundle_arch", "expect_ok"),
    [("amd64", True), ("arm64", False)],
    ids=["일치", "불일치"],
)
def test_installer_blocks_a_mismatched_bundle(
    bundle_arch: str, expect_ok: bool, tmp_path: Path
) -> None:
    """문자열 검사만으로는 '대조한다'는 것만 알 뿐 '막는다'는 것은 모른다."""
    host = subprocess.run(
        ["bash", "-c", f". {PORTABLE}; host_arch"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # 이 머신이 amd64 라는 전제 위에서만 성립하는 시험이 되지 않게 뒤집어 만든다.
    other = "arm64" if host == "amd64" else "amd64"
    written = host if expect_ok else other

    for name in ("install.sh", "selftest.sh", "portable.sh"):
        (tmp_path / name).write_bytes((AIRGAP / name).read_bytes())
    (tmp_path / "installer").mkdir()
    for name in ("server.py", "index.html"):
        (tmp_path / "installer" / name).write_bytes((AIRGAP / "installer" / name).read_bytes())
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "plan.json").write_text('{"blocks":[],"images":[],"infra":[]}\n', encoding="utf-8")
    (tmp_path / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    (tmp_path / "ARCH").write_text(f"{written}\n", encoding="utf-8")
    (tmp_path / "images.tar").write_bytes(b"x" * 1024)
    subprocess.run(
        ["bash", "-c", f". {PORTABLE}; sorted_files . | sha256_list > SHA256SUMS"],
        cwd=tmp_path,
        check=True,
    )

    result = subprocess.run(
        ["bash", str(tmp_path / "install.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    output = result.stdout + result.stderr
    if expect_ok:
        assert f"아키텍처 {written}" in output
        assert "맞는 번들을 반입한다" not in output
    else:
        assert "맞는 번들을 반입한다" in output, f"불일치를 그냥 통과시켰다:\n{output}"
        assert result.returncode != 0
