"""로깅 규약 — 예약어 충돌은 성공 경로에서 터진다.

``logger.info("...", extra={"filename": ...})``는 파이썬 표준 로깅이 ``KeyError``로
거부한다. 그런데 이 오류는 **정상 동작 중에만** 발생한다 — 실패 경로에는 대개
그 로그가 없으므로 테스트가 오류 케이스만 덮으면 영영 안 걸린다.

실제로 문서 파서에서 이 사고가 났다. 파싱에 성공할 때마다 죽는 코드였다.
같은 실수가 다시 들어오지 못하게 레포 전체를 훑는다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

SEARCH_ROOTS = ("libs", "blocks", "tools")
EXTRA_PATTERN = re.compile(r"extra=\{([^}]*)\}", re.S)
KEY_PATTERN = re.compile(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:')


def _sources() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        files.extend(
            path
            for path in (REPO_ROOT / root).rglob("*.py")
            if "__pycache__" not in path.parts and "tests" not in path.parts
        )
    return sorted(files)


def test_로그_extra에_예약어를_쓰지_않는다() -> None:
    violations: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for match in EXTRA_PATTERN.finditer(text):
            line = text[: match.start()].count("\n") + 1
            for key in KEY_PATTERN.findall(match.group(1)):
                if key in RESERVED:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{line} — extra 키 '{key}'는 "
                        "LogRecord 예약어라 로깅이 KeyError로 죽는다"
                    )
    assert not violations, "\n" + "\n".join(violations)


def test_예약어_목록이_실제로_거부되는지_확인한다() -> None:
    """이 테스트가 검사하는 규칙 자체가 참인지 못 박는다.
    파이썬이 동작을 바꾸면 위 테스트는 의미 없는 통과가 된다.

    **레벨을 INFO로 올려야 재현된다.** 기본 레벨(WARNING)에서는 ``info()``가
    LogRecord를 만들지도 않고 조용히 반환한다 — 이 버그가 숨어 있던 이유가
    바로 이것이다. 운영 설정(INFO)에서만 터진다.
    """
    import pytest

    logger = logging.getLogger("contract-probe")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)

    with pytest.raises(KeyError):
        logger.info("probe", extra={"filename": "x"})

    logger.info("probe", extra={"source_name": "x"})  # 예약어가 아니면 통과한다
