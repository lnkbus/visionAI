"""릴리스 시점 기록 — "3월에 저 고객사가 받은 게 정확히 무엇인가".

온프렘 제품에서 이 질문에 답하지 못하면 장애 대응이 성립하지 않는다. 고객사는
자기가 언제 무엇을 받았는지 모르고, 우리도 그 시점의 코드를 되짚을 수 없다.
버전 문자열만으로는 부족하다 — `0.3.0`을 표방하는 서로 다른 물건이 생길 수
있다는 것이 형상 관리의 출발점이었다(docs/07 §2).

그래서 **시점마다 형상 해시를 함께 박제**한다. 기록되는 것:

* 릴리스 버전과 날짜 — 사람이 부르는 이름
* 깃 커밋 — 코드로 되돌아가는 유일한 좌표
* 패키지 4종의 형상 해시 — 납품본과 한 줄로 대조
* 블록별 버전 — "그때 STT는 몇이었나"

:mod:`blockctl.freeze`와 역할이 다르다. 형상 명세는 **현재**를 말하고 매번
다시 계산되지만, 이력은 **과거**를 말하고 한 번 적히면 바뀌지 않는다. 그래서
freeze가 피한 타임스탬프를 여기서는 넣는다 — 다시 계산되지 않으니 diff를
흐리지 않고, 오히려 날짜가 없으면 이력이 이력 노릇을 못 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HISTORY_PATH = Path("deploy/release/history.yaml")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

HEADER = (
    "# 릴리스 시점 이력 — append-only. 지난 항목은 고치지 않는다.\n"
    "# 새 시점은 `blockctl snapshot <버전> --write`로 추가하고, 같은 커밋에\n"
    "# `git tag v<버전>`을 붙인다. 태그가 없으면 커밋 해시만이 좌표다.\n"
)


class HistoryError(RuntimeError):
    """이력 무결성 위반."""


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    released_at: str
    commit: str
    packages: dict[str, str]
    blocks: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "released_at": self.released_at,
            "commit": self.commit,
            "packages": dict(sorted(self.packages.items())),
        }
        if self.blocks:
            payload["blocks"] = dict(sorted(self.blocks.items()))
        if self.note:
            payload["note"] = self.note
        return payload


def version_key(version: str) -> tuple[int, int, int, int]:
    """정렬용 키. 프리릴리스는 같은 번호의 정식판보다 앞선다."""
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return (major, minor, patch, 0 if pre else 1)


def load_history(path: Path) -> list[ReleaseEntry]:
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("releases") or []
    if not isinstance(entries, list):
        raise HistoryError(f"{path}: releases가 목록이 아니다")
    loaded: list[ReleaseEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            raise HistoryError(f"{path}: 항목이 매핑이 아니다 — {item!r}")
        missing = {"version", "released_at", "commit", "packages"} - set(item)
        if missing:
            raise HistoryError(f"{path}: 항목에 {sorted(missing)}이 없다 — {item!r}")
        loaded.append(
            ReleaseEntry(
                version=str(item["version"]),
                released_at=str(item["released_at"]),
                commit=str(item["commit"]),
                packages={str(k): str(v) for k, v in (item["packages"] or {}).items()},
                blocks={str(k): str(v) for k, v in (item.get("blocks") or {}).items()},
                note=str(item.get("note", "")),
            )
        )
    return loaded


def verify(entries: list[ReleaseEntry]) -> list[str]:
    """이력이 이력으로 성립하는지. 문제를 문자열 목록으로 돌려준다."""
    problems: list[str] = []
    seen: set[str] = set()
    previous: ReleaseEntry | None = None

    for entry in entries:
        where = f"{entry.version}"
        if not VERSION_RE.match(entry.version):
            problems.append(f"{where}: 버전 형식이 아니다 (0.3.0 / 0.3.0-rc.1)")
            continue
        if entry.version in seen:
            problems.append(f"{where}: 버전이 중복된다 — 같은 이름의 서로 다른 물건이 생긴다")
        seen.add(entry.version)

        if not DATE_RE.match(entry.released_at):
            problems.append(f"{where}: 날짜 형식이 아니다 (YYYY-MM-DD)")
        if not COMMIT_RE.match(entry.commit):
            problems.append(f"{where}: 커밋이 40자리 해시가 아니다 — 되돌아갈 좌표가 없다")
        if not entry.packages:
            problems.append(f"{where}: 형상 해시가 비어 있다 — 납품본과 대조할 수 없다")
        for name, digest in sorted(entry.packages.items()):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                problems.append(f"{where}: {name} 형상 해시가 64자리 hex가 아니다")

        if previous is not None:
            if version_key(entry.version) <= version_key(previous.version):
                problems.append(
                    f"{where}: 버전이 {previous.version} 뒤로 갔다 — 이력 순서가 깨진다"
                )
            if entry.released_at < previous.released_at:
                problems.append(f"{where}: 날짜가 {previous.released_at}보다 이르다")
        previous = entry

    return problems


def append(entries: list[ReleaseEntry], new: ReleaseEntry) -> list[ReleaseEntry]:
    """새 시점을 뒤에 붙인다. 무결성이 깨지면 붙이지 않는다."""
    candidate = [*entries, new]
    problems = verify(candidate)
    if problems:
        raise HistoryError("\n".join(problems))
    return candidate


def render(entries: list[ReleaseEntry]) -> str:
    body = yaml.safe_dump(
        {"releases": [entry.to_dict() for entry in entries]},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return HEADER + body


def find(entries: list[ReleaseEntry], version: str) -> ReleaseEntry | None:
    return next((entry for entry in entries if entry.version == version), None)
