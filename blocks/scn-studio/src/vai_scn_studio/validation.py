"""저작 입력 검증.

잘못된 정규식이 배포되면 필터 블록이 기동에 실패하거나 룰이 조용히 무시된다.
저작 시점에 막는 것이 유일하게 안전한 지점이다.
"""

from __future__ import annotations

import re

MAX_PATTERN_LEN = 500


def validate_pattern(pattern: str) -> str | None:
    """정규식이 쓸 만한지 검사한다. 문제가 있으면 사유, 없으면 None."""
    if not pattern.strip():
        return "패턴이 비어 있다"
    if len(pattern) > MAX_PATTERN_LEN:
        return f"패턴이 너무 길다 (최대 {MAX_PATTERN_LEN}자)"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return str(exc)
    # 빈 문자열에 매칭되는 패턴은 모든 발화에 걸려 룰 체크를 무의미하게 만든다.
    if compiled.match(""):
        return "빈 문자열에 매칭되는 패턴은 모든 발화에 걸린다"
    return None
