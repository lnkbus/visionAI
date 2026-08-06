"""Makefile 이 스스로를 덮어쓰지 않는가.

make 는 같은 타깃이 두 번 정의되면 **뒤엣것을 쓰고 앞엣것을 버린다.** 경고를
한 줄 내지만 빌드 출력에 묻히고, 결과는 "돌린다고 생각한 검사가 안 도는" 상태다.

실제로 그랬다: `chart` 가 두 번 정의돼 있었고, 뒤에 있던 약한 쪽(오버레이 3종,
스키마 검증 없음)이 앞의 강한 쪽(오버레이 5종 + kubeconform)을 덮고 있었다.
`make chart` 는 초록으로 끝나는데 쿠버네티스 스키마는 아무도 안 보고 있었다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"

_TARGET = re.compile(r"^([a-zA-Z][\w-]*)\s*:(?!=)", re.M)
"""``:=`` 는 변수 대입이라 제외한다."""


def _targets() -> list[str]:
    return _TARGET.findall(MAKEFILE.read_text("utf-8"))


def test_타깃을_찾았다() -> None:
    """정규식이 낡으면 아래 대조가 조용히 통과한다."""
    targets = _targets()

    assert "up" in targets and "check" in targets, targets[:10]


def test_같은_타깃이_두_번_정의되지_않는다() -> None:
    """뒤엣것이 앞엣것을 덮는다 — 돌린다고 생각한 검사가 안 돈다."""
    duplicates = sorted(name for name, count in Counter(_targets()).items() if count > 1)

    assert not duplicates, f"중복 타깃: {duplicates} — 뒤에 있는 정의만 실행된다"


def test_phony_에_적힌_타깃이_실제로_있다() -> None:
    """없는 타깃이 .PHONY 에 남아 있으면, 지운 줄 모르고 계속 부르게 된다."""
    text = MAKEFILE.read_text("utf-8")
    declared = re.search(r"^\.PHONY:\s*(.+)$", text, re.M)

    assert declared is not None, ".PHONY 선언이 없다"
    missing = sorted(set(declared.group(1).split()) - set(_targets()))

    assert not missing, f".PHONY 에만 있는 타깃: {missing}"
