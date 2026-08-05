"""블록이 **자기 이미지 하나로** 뜰 수 있는가.

개발 워크스페이스는 모든 패키지의 의존성이 한 venv 에 섞여 있다. 그래서
`uvicorn` 을 의존성에 안 적은 블록도 개발 중에는 잘 돈다 — 다른 패키지가
깔아 둔 것을 빌려 쓰기 때문이다.

블록 이미지는 `uv sync --package vai-<블록>` 로 그 블록 것만 설치한다. 거기서
빠진 의존성은 **기동 순간에** 드러난다:

    ModuleNotFoundError: No module named 'uvicorn'

컨테이너가 크래시 루프에 빠지고, CI 는 이미지를 *빌드만* 하지 돌려 보지
않으므로 아무도 모른 채 납품된다. 실제로 AUD-VAD 와 STT-CORE 가 그 상태였고,
에어갭 설치 시연에서 처음 드러났다.

여기서는 **선언만** 본다. 실제 기동은 이미지 시험의 몫이지만, 선언이 틀린 것은
정적으로 잡을 수 있고 그게 훨씬 싸다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLOCKS = sorted(p.parent for p in (ROOT / "blocks").glob("*/pyproject.toml"))

SERVER_DEPS = ("fastapi", "uvicorn")
"""`vai_common.service.serve` 가 쓰는 것. serve 를 부르면 이 둘이 필요하다."""


def _declared(block: Path) -> str:
    data = tomllib.loads((block / "pyproject.toml").read_text("utf-8"))
    project = data["project"]
    parts = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        parts += list(extra)
    return " ".join(parts)


def _starts_server(block: Path) -> bool:
    for path in (block / "src").rglob("*.py"):
        text = path.read_text("utf-8")
        if "serve(" in text and "vai_common.service" in text:
            return True
    return False


def test_블록을_찾았다() -> None:
    """경로가 바뀌면 이 파일 전체가 조용히 통과한다."""
    assert len(BLOCKS) >= 15


@pytest.mark.parametrize("block", BLOCKS, ids=lambda p: p.name)
def test_서버를_띄우는_블록은_서버_의존성을_선언한다(block: Path) -> None:
    if not _starts_server(block):
        pytest.skip("HTTP 서버를 띄우지 않는 블록")

    declared = _declared(block)
    missing = [name for name in SERVER_DEPS if name not in declared]

    assert not missing, (
        f"{block.name}: {', '.join(missing)} 를 선언하지 않았다 — "
        "개발 워크스페이스에서는 남의 것을 빌려 쓰다가 블록 이미지에서 죽는다"
    )


@pytest.mark.parametrize("block", BLOCKS, ids=lambda p: p.name)
def test_공통_계약_의존성을_선언한다(block: Path) -> None:
    declared = _declared(block)

    assert "vai-contracts" in declared, f"{block.name}: 계약 패키지를 선언하지 않았다"
