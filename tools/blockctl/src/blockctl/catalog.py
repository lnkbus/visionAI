"""블록 카탈로그 로딩과 조립 검증.

문서(docs/00-module-catalog.md)가 사람이 읽는 카탈로그라면, 여기는 기계가 읽는
카탈로그다. 둘이 어긋나면 조립·견적·라이선스가 전부 어긋나므로 CI가 검사한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from vai_contracts.block import BlockManifest


@dataclass(frozen=True)
class LoadedBlock:
    manifest: BlockManifest
    path: Path

    @property
    def directory(self) -> str:
        return self.path.parent.name


class CatalogError(Exception):
    """카탈로그 정합성 위반."""


def load_catalog(blocks_dir: Path) -> list[LoadedBlock]:
    blocks: list[LoadedBlock] = []
    for manifest_path in sorted(blocks_dir.glob("*/block.yaml")):
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        try:
            manifest = BlockManifest.model_validate(raw)
        except ValidationError as exc:
            raise CatalogError(f"{manifest_path}: 매니페스트 스키마 위반\n{exc}") from exc
        blocks.append(LoadedBlock(manifest, manifest_path))
    return blocks


def validate(blocks: list[LoadedBlock]) -> list[str]:
    """정합성 문제를 문자열 목록으로 돌려준다(빈 목록이면 통과)."""
    problems: list[str] = []
    by_id = {b.manifest.id: b for b in blocks}

    if len(by_id) != len(blocks):
        problems.append("블록 ID가 중복되었다")

    for block in blocks:
        manifest = block.manifest

        for dep in manifest.depends_on:
            if dep not in by_id:
                problems.append(f"{manifest.id}: 알 수 없는 의존 블록 '{dep}'")

        # 아무도 만들지 않는 토픽을 소비하겠다는 선언은 조립 실수다.
        produced = {t for b in blocks for t in b.manifest.contracts.produces}
        for topic in manifest.contracts.consumes:
            if topic not in produced:
                problems.append(f"{manifest.id}: '{topic}' 토픽의 생산자가 카탈로그에 없다")

        billing = manifest.billing
        if not billing.included_in_package and billing.capacity_unit.value == "none":
            problems.append(
                f"{manifest.id}: 패키지 포함이 아니면서 용량 단위가 없다 — 청구할 수 없는 블록이다"
            )

    problems.extend(_detect_cycles(by_id))
    return problems


def _detect_cycles(by_id: dict[str, LoadedBlock]) -> list[str]:
    """의존 순환 탐지. 순환이 있으면 기동 순서를 정할 수 없다."""
    problems: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            problems.append(f"의존 순환: {' → '.join([*trail, node])}")
            return
        visiting.add(node)
        block = by_id.get(node)
        if block is not None:
            for dep in block.manifest.depends_on:
                walk(dep, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for block_id in by_id:
        walk(block_id, [])
    return problems
