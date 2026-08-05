"""패키지 형상 고정 — 무엇을 납품했는지 기계가 증명한다.

GS인증·공공 조달에서 가장 먼저 요구되는 것은 기능이 아니라 **형상 식별**이다:
이 제품이 정확히 어떤 구성요소의 어떤 버전으로 이루어져 있는가, 그리고 심사에
제출한 것과 현장에 설치된 것이 같은가.

손으로 적은 구성요소 목록은 반드시 어긋난다. 블록 하나를 올려도 문서는 그대로
남고, 그 사실은 심사 당일이나 현장 검수에서 드러난다. 그래서 이 명세는
**카탈로그와 Helm 오버레이에서 계산**하고, 어긋나면 CI가 실패한다.

포함하는 것과 그 이유:

* **구성요소와 버전** — 형상 식별의 기본.
* **소스 해시** — 버전 문자열은 사람이 올려 주지 않으면 안 바뀐다. 내용이
  바뀌었는데 버전이 그대로인 상태를 잡아내려면 내용 자체의 해시가 필요하다.
* **라이선스 게이트 키·청구 단위** — 납품 범위와 계약 범위가 같은지 대조하는 근거.
* **인프라 이미지** — 반입 대상 전체. 하나 빠지면 폐쇄망에서 기동하지 않는다.
* **전체 형상 해시** — 제출본과 설치본을 한 줄로 대조한다.

시각(타임스탬프)은 넣지 않는다. 넣으면 내용이 하나도 안 바뀐 실행에서도 파일이
달라져, 형상이 실제로 바뀌었는지를 diff가 말해 주지 못한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from blockctl.catalog import LoadedBlock

SOURCE_GLOBS = ("block.yaml", "pyproject.toml", "src/**/*.py", "contracts/**/*.yaml")
"""해시 대상. 실행에 관여하는 것만 넣는다.

테스트는 제외한다 — 납품물에 들어가지 않고, 테스트만 고쳐도 형상 해시가
바뀌면 "형상이 바뀌었다"는 신호가 무의미해진다.
"""

LIBRARY_DIRS = ("contracts", "common", "retrieval")


class FreezeError(RuntimeError):
    """형상 명세를 만들 수 없다."""


@dataclass(frozen=True)
class Component:
    """형상 항목 하나."""

    component_id: str
    name: str
    version: str
    kind: str
    """``block`` · ``library`` · ``infra``."""

    source_hash: str = ""
    license_key: str = ""
    capacity_unit: str = ""
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.component_id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
        }
        if self.source_hash:
            payload["source_hash"] = self.source_hash
        if self.license_key:
            payload["license_key"] = self.license_key
        if self.capacity_unit:
            payload["capacity_unit"] = self.capacity_unit
        if self.depends_on:
            payload["depends_on"] = sorted(self.depends_on)
        return payload


@dataclass(frozen=True)
class PackageFreeze:
    package: str
    chart_version: str
    app_version: str
    components: list[Component]

    @property
    def configuration_hash(self) -> str:
        """전체 형상 해시. 제출본과 설치본을 이 한 줄로 대조한다."""
        digest = hashlib.sha256()
        for component in self.components:
            digest.update(
                f"{component.component_id}\0{component.version}\0{component.source_hash}\0".encode()
            )
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "_comment": (
                "패키지 형상 명세 — blockctl freeze가 생성한다. 손으로 고치지 않는다. "
                "구성이 바뀌면 `blockctl freeze --write`로 다시 만들고 그 diff를 검토한다."
            ),
            "package": self.package,
            "chart_version": self.chart_version,
            "app_version": self.app_version,
            "configuration_hash": self.configuration_hash,
            "component_count": len(self.components),
            "components": [component.to_dict() for component in self.components],
        }


def source_hash(root: Path) -> str:
    """디렉토리 내용의 해시. 경로와 내용을 함께 넣어 이름 변경도 잡는다."""
    digest = hashlib.sha256()
    seen: set[Path] = set()
    for pattern in SOURCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts or path in seen:
                continue
            seen.add(path)
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def enabled_blocks(values_path: Path) -> set[str]:
    """Helm 오버레이에서 켜진 블록 디렉토리 이름을 읽는다.

    카탈로그가 아니라 **실제 배포 설정**을 근거로 삼는다. 카탈로그에 있는 모든
    블록을 적으면 그건 제품 형상이 아니라 개발 저장소의 목록이다.
    """
    if not values_path.is_file():
        raise FreezeError(f"패키지 오버레이를 찾을 수 없다: {values_path}")
    raw = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
    blocks = raw.get("blocks") or {}
    return {name for name, cfg in blocks.items() if (cfg or {}).get("enabled")}


def chart_versions(chart_path: Path) -> tuple[str, str]:
    if not chart_path.is_file():
        raise FreezeError(f"Chart.yaml을 찾을 수 없다: {chart_path}")
    raw = yaml.safe_load(chart_path.read_text(encoding="utf-8")) or {}
    return str(raw.get("version", "")), str(raw.get("appVersion", ""))


def freeze_package(
    package: str,
    blocks: list[LoadedBlock],
    *,
    root: Path,
    chart_dir: Path,
) -> PackageFreeze:
    """패키지 하나의 형상을 계산한다."""
    values_path = chart_dir / f"values-{package}.yaml"
    wanted = enabled_blocks(values_path)
    by_directory = {block.directory: block for block in blocks}

    missing = sorted(wanted - set(by_directory))
    if missing:
        # 오버레이가 켜 둔 블록이 카탈로그에 없다 — 렌더링은 되지만 이미지가 없다.
        raise FreezeError(f"오버레이가 켠 블록이 카탈로그에 없다: {missing}")

    components: list[Component] = []
    infra: set[str] = set()
    for directory in sorted(wanted):
        block = by_directory[directory]
        manifest = block.manifest
        infra.update(manifest.infra)
        components.append(
            Component(
                component_id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                kind="block",
                source_hash=source_hash(block.path.parent),
                license_key=manifest.gate_key,
                capacity_unit=manifest.billing.capacity_unit.value,
                depends_on=list(manifest.depends_on),
            )
        )

    for library in LIBRARY_DIRS:
        lib_path = root / "libs" / library
        if not lib_path.is_dir():
            continue
        components.append(
            Component(
                component_id=f"lib:{library}",
                name=f"vai_{library}",
                version=_library_version(lib_path),
                kind="library",
                source_hash=source_hash(lib_path),
            )
        )

    components.extend(
        Component(component_id=f"infra:{name}", name=name, version="", kind="infra")
        for name in sorted(infra)
    )

    chart_version, app_version = chart_versions(chart_dir / "Chart.yaml")
    return PackageFreeze(
        package=package,
        chart_version=chart_version,
        app_version=app_version,
        components=components,
    )


def _library_version(directory: Path) -> str:
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def render(freeze: PackageFreeze) -> str:
    return yaml.safe_dump(freeze.to_dict(), allow_unicode=True, sort_keys=False)


def diff(current: PackageFreeze, recorded: dict[str, Any]) -> list[str]:
    """기록된 형상과 지금 계산한 형상의 차이. 심사 제출본과의 대조에 그대로 쓴다."""
    problems: list[str] = []
    if recorded.get("configuration_hash") == current.configuration_hash:
        return problems

    was = {str(item["id"]): item for item in recorded.get("components", [])}
    now = {component.component_id: component.to_dict() for component in current.components}

    for component_id in sorted(set(was) - set(now)):
        problems.append(f"빠짐: {component_id} ({was[component_id].get('version', '')})")
    for component_id in sorted(set(now) - set(was)):
        problems.append(f"추가됨: {component_id} ({now[component_id].get('version', '')})")
    for component_id in sorted(set(was) & set(now)):
        before, after = was[component_id], now[component_id]
        if before.get("version") != after.get("version"):
            problems.append(
                f"버전 변경: {component_id} {before.get('version')} → {after.get('version')}"
            )
        elif before.get("source_hash") != after.get("source_hash"):
            # 버전은 그대로인데 내용이 바뀐 경우. 형상 관리에서 가장 위험한 상태다 —
            # 같은 버전을 표방하는 두 개의 다른 물건이 존재하게 된다.
            problems.append(
                f"내용 변경(버전 그대로): {component_id} {before.get('version')} — "
                "버전을 올리거나 형상 명세를 갱신한다"
            )

    if not problems:
        problems.append("형상 해시가 다른데 구성요소 차이를 특정하지 못했다 — 차트 버전 확인")
    return problems
