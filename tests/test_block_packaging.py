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

import ast
import re
import sys
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


# 모듈 이름과 배포 이름이 다른 것들. 이 표에 없으면 밑줄→하이픈 규칙으로 본다.
MODULE_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "multipart": "python-multipart",
    "PIL": "pillow",
}


def _requirement_names(parts: list[str]) -> set[str]:
    """`"vai-common"`, `"fastapi>=0.115"`, `"uvicorn[standard]>=0.30"` → 이름만."""
    names = set()
    for raw in parts:
        name = re.split(r"[<>=!~\[; ]", raw.strip(), maxsplit=1)[0]
        if name:
            names.add(name.replace("_", "-").lower())
    return names


def _direct_requirements(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text("utf-8"))
    project = data["project"]
    parts = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        parts += list(extra)
    return _requirement_names(parts)


def _workspace_packages() -> dict[str, Path]:
    found = {}
    for pyproject in (*ROOT.glob("libs/*/pyproject.toml"), *ROOT.glob("blocks/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text("utf-8"))
        found[data["project"]["name"].replace("_", "-").lower()] = pyproject
    return found


def _reachable(block: Path) -> set[str]:
    """블록 이미지에 실제로 깔리는 배포들.

    직접 선언한 것 + **워크스페이스 라이브러리를 타고 들어오는 것**이다.
    `vai-common` 을 선언하면 그것이 끌고 오는 pydantic·redis 도 함께 깔린다.
    그것까지 다시 적으라고 하면 아무도 이 시험을 안 지킨다.
    """
    workspace = _workspace_packages()
    seen: set[str] = set()
    queue = list(_direct_requirements(block / "pyproject.toml"))
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in workspace:
            queue += _direct_requirements(workspace[name])
    return seen


def _guarded(tree: ast.AST) -> set[int]:
    """`try: import X / except ImportError:` 안에 있는 import 들의 위치.

    선택 엔진은 없을 수도 있는 것이 정상이고, 그 사실을 코드가 이미 다루고
    있다. 여기서 문제 삼는 것은 **없으면 그냥 죽는** import 다.
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import = any(
            isinstance(handler.type, ast.Name) and handler.type.id in {"ImportError", "Exception"}
            for handler in node.handlers
        )
        if not catches_import:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import | ast.ImportFrom):
                inside.add(id(child))
    return inside


def _unguarded_imports(block: Path) -> dict[str, Path]:
    """이 블록이 **반드시 있어야 하는** 바깥 모듈 → 처음 쓴 파일."""
    own = {path.name for path in (block / "src").iterdir() if path.is_dir()}
    found: dict[str, Path] = {}
    for path in sorted((block / "src").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        guarded = _guarded(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                if module in sys.stdlib_module_names or module in own:
                    continue
                found.setdefault(module, path.relative_to(ROOT))
    return found


@pytest.mark.parametrize("block", BLOCKS, ids=lambda p: p.name)
def test_import_하는_것을_전부_선언한다(block: Path) -> None:
    """`fastapi`·`uvicorn` 만 보던 것을 **실제 import 전부**로 넓힌다.

    이 결함이 되풀이됐다: TA-ASSIST 가 `vai_retrieval` 을, RAG-KB 가
    `qdrant_client` 를, UI-MEET 이 `websockets` 를 선언 없이 썼다. 개발
    워크스페이스에서는 형제 패키지가 깔아 둔 것을 빌려 써서 전부 잘 돌고,
    블록 이미지에서만 `ModuleNotFoundError` 로 죽는다. CI 는 이미지를
    빌드만 하고 돌려 보지 않으니 그대로 납품된다.

    `try/except ImportError` 로 감싼 것은 뺀다 — 없을 수 있다는 사실을
    코드가 이미 다루고 있다(선택 엔진들이 그렇다).
    """
    reachable = _reachable(block)
    missing = {
        module: where
        for module, where in _unguarded_imports(block).items()
        if MODULE_TO_DISTRIBUTION.get(module, module.replace("_", "-").lower()) not in reachable
    }

    assert not missing, (
        f"{block.name}: 선언 없이 import 한다 — "
        + ", ".join(f"{module} ({where})" for module, where in sorted(missing.items()))
        + ". pyproject.toml 에 넣는다. 개발에서는 남의 것을 빌려 쓰다가 이미지에서 죽는다"
    )
