"""재기동을 넘어 남아야 하는 상태가 실제로 남는가 — 레포 전체 게이트.

블록별 감사에서 나온 결함 중 **가장 많이 반복된 한 가지 모양**이 있다:
상태를 메모리에만 두고, 그 사실이 아무 데서도 드러나지 않는 것.

    LLM-SUM   회의록이 7일 뒤 조용히 사라졌다
    UI-MEET   사람이 입력한 참석자 실명이 재기동으로 사라졌다
    RAG-KB    문서 메타가 사라져 파기 요청은 404인데 색인은 계속 답했다
    RAG-SRCH  희소 색인이 비어 검색이 dense 절반만으로 계속 응답했다

넷 다 예외를 던지지 않고, 로그도 남기지 않고, 테스트도 통과했다. 기능 시험으로
잡을 수 있는 종류가 아니다 — **한 프로세스 안에서는 전부 정상으로 보이기**
때문이다. 그래서 기능이 아니라 배선의 모양을 검사한다.

여기서 세 가지를 본다:

1. 휘발 구현에는 영속 짝이 있는가(:func:`test_volatile_store_has_durable_sibling`)
2. 프로덕션 배선이 그 영속 짝을 고를 수 있는가(:func:`test_wiring_offers_the_durable_store`)
3. 배경 태스크를 만들고 버리지 않는가(:func:`test_background_tasks_are_held`)

셋 다 정적 검사다. 실행하지 않고 소스만 읽는다 — 이 결함들은 "돌려 보면
정상"이 특징이라 실행으로는 잡히지 않기 때문이다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

VOLATILE_PREFIXES = ("InMemory", "Memory")
"""휘발 구현임을 이름으로 선언하는 접두사."""

DURABLE_PREFIXES = ("Redis", "File", "Qdrant", "Postgres", "Sqlite", "S3")
"""프로세스 밖에 상태를 두는 구현의 접두사."""

VOLATILE_ONLY: dict[str, str] = {}
"""영속 짝이 없어도 되는 휘발 구현 — **이유를 함께 적는다**.

비워 두는 것이 기본이다. 여기에 한 줄을 더하는 것은 "이 상태는 재기동으로
사라져도 된다"는 판단을 남기는 행위이고, 그 판단은 리뷰에서 보여야 한다.
"""

WIRING_FILES = ("main.py", "app.py")
"""프로덕션 진입점. 여기서 무엇을 고르는지가 실제 운영 동작이다."""

TASK_FACTORIES = ("create_task", "ensure_future")


# ── 소스 수집 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    path: Path
    tree: ast.Module

    @property
    def package(self) -> Path:
        """``blocks/llm-sum/src/vai_llm_sum`` — 짝을 찾는 범위."""
        parts = self.path.parts
        return Path(*parts[: parts.index("src") + 2])

    @property
    def where(self) -> str:
        return str(self.path.relative_to(REPO))


def _sources() -> list[Source]:
    found: list[Source] = []
    for root in ("blocks", "libs"):
        for path in sorted((REPO / root).glob("*/src/*/**/*.py")):
            if "tests" in path.parts:
                continue
            found.append(Source(path, ast.parse(path.read_text(encoding="utf-8"))))
    assert found, "소스를 하나도 못 찾았다 — 경로 규칙이 낡았다"
    return found


SOURCES = _sources()


def _noun(name: str, prefixes: tuple[str, ...]) -> str | None:
    """``InMemorySummaryStore`` → ``SummaryStore``. 접두사가 없으면 None."""
    for prefix in prefixes:
        rest = name.removeprefix(prefix)
        if rest != name and rest[:1].isupper():
            return rest
    return None


def _classes(source: Source) -> list[str]:
    return [node.name for node in ast.walk(source.tree) if isinstance(node, ast.ClassDef)]


def _constructed(tree: ast.AST) -> set[str]:
    """실제로 만들어진 클래스 이름.

    import만 남고 호출이 사라진 경우를 걸러야 하므로 **호출**을 본다.
    ``RedisEventBus.from_url(...)`` 처럼 대체 생성자를 쓰는 경우가 있어
    ``Name(...)`` 과 ``Name.method(...)`` 를 모두 센다.
    """
    made: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            made.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            made.add(func.value.id)
    return made


# ── 1. 휘발 구현에는 영속 짝이 있는가 ───────────────────────────────────────


def test_volatile_store_has_durable_sibling() -> None:
    """``InMemoryX``만 있고 ``RedisX``/``FileX``가 없으면 그 상태는 갈 곳이 없다.

    네 결함 전부 이 모양이었다. 인메모리 구현만 있으니 배선은 그것을 고를
    수밖에 없었고, 고른 쪽도 고르지 않은 쪽도 아무 신호를 내지 않았다.
    """
    durable_by_package: dict[Path, set[str]] = {}
    for source in SOURCES:
        for name in _classes(source):
            noun = _noun(name, DURABLE_PREFIXES)
            if noun:
                durable_by_package.setdefault(source.package, set()).add(noun)

    orphans: list[str] = []
    for source in SOURCES:
        for name in _classes(source):
            noun = _noun(name, VOLATILE_PREFIXES)
            if noun is None or name in VOLATILE_ONLY:
                continue
            if noun not in durable_by_package.get(source.package, set()):
                orphans.append(f"{source.where}: {name} (짝이 될 {noun} 없음)")

    assert not orphans, (
        "휘발 구현만 있고 영속 구현이 없다 — 이 상태는 재기동으로 사라진다:\n  "
        + "\n  ".join(orphans)
        + "\n영속이 필요 없다면 VOLATILE_ONLY에 이유와 함께 적는다."
    )


def test_volatile_only_entries_still_exist() -> None:
    """면제 목록이 유령을 가리키면 다음 사람이 그것을 근거로 삼는다."""
    declared = {name for source in SOURCES for name in _classes(source)}
    stale = sorted(set(VOLATILE_ONLY) - declared)
    assert not stale, f"VOLATILE_ONLY에 없는 클래스가 남아 있다: {stale}"


# ── 2. 프로덕션 배선이 영속 짝을 고를 수 있는가 ─────────────────────────────


def test_wiring_offers_the_durable_store() -> None:
    """구현을 만들어 놓고 배선에서 안 쓰면 없는 것과 같다.

    운영에서 인메모리로 내려가는 것 자체는 막지 않는다 — Redis가 없는 개발
    환경이 있어야 한다. 다만 **인메모리밖에 고를 수 없는 배선**은 막는다.
    """
    blind: list[str] = []
    for source in SOURCES:
        if source.path.name not in WIRING_FILES:
            continue
        made = _constructed(source.tree)
        durable_here = {
            noun for name in made if (noun := _noun(name, DURABLE_PREFIXES)) is not None
        }
        for name in sorted(made):
            noun = _noun(name, VOLATILE_PREFIXES)
            if noun and noun not in durable_here:
                blind.append(f"{source.where}: {name}만 배선돼 있다 (영속 선택지 없음)")

    assert not blind, (
        "진입점이 휘발 구현만 고른다 — 배포하면 상태가 재기동마다 사라진다:\n  "
        + "\n  ".join(blind)
    )


# ── 3. 배경 태스크를 만들고 버리지 않는가 ───────────────────────────────────


def _discarded_tasks(source: Source) -> list[str]:
    """참조를 남기지 않는 ``create_task`` 호출 위치.

    두 가지를 본다. 값을 아예 안 받는 ``asyncio.create_task(...)`` 한 줄과,
    변수에 담고 그 변수를 두 번 다시 쓰지 않는 경우다. 둘 다 결과는 같다 —
    태스크가 예외로 죽어도 아무도 모르고, GC가 가져가면 조용히 멈춘다.
    """

    def is_factory(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in TASK_FACTORIES

    # 노드마다 "어느 함수 안에 있는가"를 알아야 변수 재사용 여부를 볼 수 있다.
    # 트리는 한 번만 훑는다 — 스코프마다 다시 훑으면 중첩 함수의 같은 줄이
    # 두 번 잡힌다.
    scope_of: dict[ast.AST, ast.AST] = {}
    scopes: list[ast.AST] = [source.tree]

    def assign_scopes(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef | ast.Lambda):
                scopes.append(child)
                assign_scopes(child, child)
            else:
                scope_of[child] = scope
                assign_scopes(child, scope)

    assign_scopes(source.tree, source.tree)
    loaded: dict[ast.AST, set[str]] = {
        scope: {
            n.id for n in ast.walk(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        for scope in scopes
    }

    found: list[str] = []
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Expr) and is_factory(node.value):
            found.append(f"{source.where}:{node.lineno} 반환값을 받지 않는다")
        elif (
            isinstance(node, ast.Assign)
            and is_factory(node.value)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id not in loaded.get(scope_of.get(node, source.tree), set())
        ):
            found.append(f"{source.where}:{node.lineno} {node.targets[0].id}를 다시 쓰지 않는다")
    return found


def test_background_tasks_are_held() -> None:
    """CORE-GW의 UI 송신 태스크가 죽어도 아무 증상이 없던 이유가 이것이다.

    참조를 놓친 태스크는 두 가지로 망가진다. 예외를 삼키고(아무도 결과를
    보지 않으므로), 이벤트 루프가 유일한 참조일 때는 GC가 가져가 버린다.
    화면에는 "연결은 되어 있는데 자막이 안 나온다"로만 나타난다.
    """
    discarded = [line for source in SOURCES for line in _discarded_tasks(source)]
    assert not discarded, (
        "만들어 놓고 잡아 두지 않는 배경 태스크가 있다 — 죽어도 무증상이다:\n  "
        + "\n  ".join(discarded)
    )


# ── 검사기 자체의 검사 ──────────────────────────────────────────────────────
#
# 위 셋은 "위반이 없다"를 주장한다. 검사기가 고장 나서 아무것도 못 찾게 되면
# 그 주장도 조용히 통과한다 — 감사에서 본 것과 정확히 같은 실패 모양이다.


@pytest.mark.parametrize(
    ("name", "prefixes", "expected"),
    [
        ("InMemorySummaryStore", VOLATILE_PREFIXES, "SummaryStore"),
        ("MemoryUsageMeter", VOLATILE_PREFIXES, "UsageMeter"),
        ("FileSummaryStore", DURABLE_PREFIXES, "SummaryStore"),
        ("QdrantVectorStore", DURABLE_PREFIXES, "VectorStore"),
        ("SummaryStore", VOLATILE_PREFIXES, None),
        # 접두사로 시작하는 것만으로는 부족하다 — 뒤가 새 단어여야 한다.
        ("Filename", DURABLE_PREFIXES, None),
    ],
)
def test_noun_extraction(name: str, prefixes: tuple[str, ...], expected: str | None) -> None:
    assert _noun(name, prefixes) == expected


@pytest.mark.parametrize(
    ("body", "hits"),
    [
        (["asyncio.create_task(g())"], 1),
        (["t = asyncio.create_task(g())"], 1),
        (["t = asyncio.create_task(g())", "await t"], 0),
        (["ts = [asyncio.create_task(g())]", "await ts[0]"], 0),
        (["ts.append(asyncio.create_task(g()))"], 0),
        (["asyncio.ensure_future(g())"], 1),
        # 중첩 함수에서 같은 줄이 두 번 세어지지 않아야 한다.
        (["async def inner():", "    asyncio.create_task(g())", "await inner()"], 1),
    ],
)
def test_task_detector_catches_the_shapes(body: list[str], hits: int) -> None:
    code = "import asyncio\nasync def f():\n" + "".join(f"    {line}\n" for line in body)
    synthetic = Source(REPO / "blocks" / "_synthetic.py", ast.parse(code))
    assert len(_discarded_tasks(synthetic)) == hits
