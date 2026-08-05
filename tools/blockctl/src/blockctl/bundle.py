"""에어갭 반입 계획.

폐쇄망 반입은 되돌릴 수 없다. USB로 들고 들어간 뒤 "이미지 하나가 빠졌다"를
알게 되면 반출입 승인 절차를 다시 밟아야 하고, 고객사에 따라 며칠이 걸린다.
그래서 **무엇을 담을지 사람이 목록으로 관리하지 않는다** — 라이선스와
카탈로그에서 계산한다.

계산 대상 셋:

1. 라이선스에 허가된 블록
2. 그 블록들의 **의존 폐포**(TA-ASSIST를 사면 CORE-BUS도 필요하다)
3. 각 블록이 선언한 인프라 이미지(redis·qdrant)

의존 폐포를 손으로 세는 것이 가장 흔한 누락 지점이다. 직접 의존은 눈에 보이지만
의존의 의존은 안 보인다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from blockctl.catalog import CatalogError, LoadedBlock

INFRA_IMAGES: dict[str, str] = {
    "redis": "redis:7-alpine",
    "qdrant": "qdrant/qdrant:v1.12.1",
}
"""인프라 이미지 태그. compose와 **같은 값**이어야 한다 — 갈라지면 번들로 설치한
환경과 개발 환경의 버전이 달라지고, 재현되지 않는 장애가 생긴다."""

ALWAYS_INCLUDED = ("CORE-LIC",)
"""라이선스에 없어도 반드시 담는다. 라이선스를 설치하는 블록이 번들에 없으면
최초 구축에서 아무것도 기동할 수 없다 — 닭과 달걀."""


@dataclass
class BundlePlan:
    blocks: list[str] = field(default_factory=list)
    """반입할 블록 ID(정렬)."""

    added_by_dependency: list[str] = field(default_factory=list)
    """라이선스에는 없지만 기동 의존·이벤트 생산자로 따라온 블록. 사람이 확인할
    수 있게 분리해 둔다 — 조용히 늘어나면 반입 승인 목록과 실제가 어긋난다."""

    added_by_recommendation: list[str] = field(default_factory=list)
    """기능 의존으로 따라온 블록. 없어도 기동하지만 기능이 죽으므로 담되,
    **라이선스 범위를 넘는 반입**이라 영업·계약과 대조할 수 있게 분리한다."""

    infra: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    """``visionai/<block>:<version>`` + 인프라 이미지."""

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "blocks": self.blocks,
            "added_by_dependency": self.added_by_dependency,
            "added_by_recommendation": self.added_by_recommendation,
            "infra": self.infra,
            "images": self.images,
            "warnings": self.warnings,
        }


def licensed_blocks(license_path: Path) -> list[str]:
    """``.lic``에서 허가된 블록 ID를 뽑는다.

    서명은 검증하지 않는다. 번들 빌드는 공급사 내부 작업이고, 검증은 고객사
    서버에서 각 블록이 한다. 여기서 공개키를 요구하면 빌드 서버에 검증 자산이
    하나 더 늘 뿐 보안은 나아지지 않는다.
    """
    raw = json.loads(license_path.read_text(encoding="utf-8"))
    payload = raw.get("payload", raw)
    return sorted(
        block_id
        for block_id, spec in dict(payload.get("blocks", {})).items()
        if dict(spec).get("enabled", True)
    )


def plan(
    blocks: list[LoadedBlock], requested: list[str], *, image_prefix: str = "visionai"
) -> BundlePlan:
    """반입 계획을 계산한다."""
    by_id = {b.manifest.id: b for b in blocks}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        # 카탈로그에 없는 블록을 담으라는 요청은 라이선스 오타이거나 미출시 블록이다.
        raise CatalogError(f"카탈로그에 없는 블록: {', '.join(unknown)}")

    # 소비하는 토픽의 생산자도 반드시 함께 담는다. 이벤트로만 이어진 블록은
    # depends_on에 안 나타나므로, 이것 없이는 "소비자만 있고 생산자가 없는"
    # 번들이 만들어진다 — 기동은 되고 아무 일도 일어나지 않는다.
    producers: dict[str, list[str]] = {}
    for block in blocks:
        for topic in block.manifest.contracts.produces:
            producers.setdefault(topic, []).append(block.manifest.id)

    seeds = sorted(set(requested) | {b for b in ALWAYS_INCLUDED if b in by_id})
    resolved: set[str] = set()
    by_recommendation: set[str] = set()
    deferred: list[tuple[str, str, list[str]]] = []
    pending = list(seeds)
    while pending:
        block_id = pending.pop()
        if block_id in resolved:
            continue
        resolved.add(block_id)
        # 폐포는 카탈로그 안에서만 확장되므로 여기서 모르는 ID가 나올 수 없다.
        # (요청 단계에서 이미 걸렀고, 의존은 아래에서 확인한다.)
        block = by_id[block_id]

        missing = [dep for dep in block.manifest.depends_on if dep not in by_id]
        if missing:
            raise CatalogError(f"{block_id}: 의존 블록이 카탈로그에 없다 — {', '.join(missing)}")

        follow = list(block.manifest.depends_on)
        for topic in block.manifest.contracts.consumes:
            candidates = producers.get(topic, [])
            if len(candidates) == 1:
                # 생산자가 하나뿐이면 그것이 없을 때 이 블록은 아무 일도 하지 않는다.
                follow.append(candidates[0])
            else:
                # 생산자가 여럿이면 전부 필요한 것이 아니다(audit.log가 그렇다 —
                # CORE-SEC은 생산자 하나만 있어도 제 일을 한다). 자동으로 다 담으면
                # 라이선스 범위 밖 이미지가 무더기로 들어간다. 폐포가 끝난 뒤
                # **하나도 안 담겼을 때만** 사람에게 알린다.
                deferred.append((block_id, topic, candidates))
        for recommended in block.manifest.recommends:
            if recommended in by_id:
                follow.append(recommended)
                by_recommendation.add(recommended)

        pending.extend(dep for dep in follow if dep not in resolved)

    infra: set[str] = set()
    for block_id in resolved:
        infra.update(by_id[block_id].manifest.infra)

    unknown_infra = sorted(infra - set(INFRA_IMAGES))
    warnings = [f"인프라 이미지 태그를 모른다: {name}" for name in unknown_infra]
    for consumer, topic, candidates in deferred:
        if candidates and set(candidates) & resolved:
            continue  # 생산자 중 하나가 이미 담겼다 — 알릴 것이 없다
        producer_hint = (
            f"생산자 {', '.join(sorted(candidates))} 중 최소 하나가 필요하다"
            if candidates
            else "생산자가 카탈로그에 없다"
        )
        warnings.append(f"{consumer}가 '{topic}'을 소비한다 — {producer_hint}")

    images = [
        f"{image_prefix}/{by_id[block_id].directory}:{by_id[block_id].manifest.version}"
        for block_id in sorted(resolved)
    ]
    images.extend(INFRA_IMAGES[name] for name in sorted(infra & set(INFRA_IMAGES)))

    recommended_only = (by_recommendation & resolved) - set(seeds)
    return BundlePlan(
        blocks=sorted(resolved),
        added_by_dependency=sorted(resolved - set(seeds) - recommended_only),
        added_by_recommendation=sorted(recommended_only),
        infra=sorted(infra),
        images=images,
        warnings=warnings,
    )


def checksum_manifest(directory: Path) -> dict[str, str]:
    """번들 내용물의 SHA-256.

    반입 매체(USB)는 손상되거나 바꿔치기될 수 있다. 설치 전에 대조하지 않으면
    반쯤 깨진 이미지를 로드하고 원인 모를 장애를 쫓게 된다.
    """
    digests: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digests[str(path.relative_to(directory))] = digest.hexdigest()
    return digests
