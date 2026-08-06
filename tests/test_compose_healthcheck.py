"""헬스체크가 그 이미지의 쉘에서 실제로 도는가.

`/dev/tcp/호스트/포트` 는 **bash 전용 문법**이다. `CMD-SHELL` 은 `/bin/sh -c` 로
돌고, 이미지에 따라 그 `sh` 가 dash·busybox 다. dash 는 저걸 진짜 파일 경로로
취급해서 이렇게 죽는다:

    sh: 1: cannot create /dev/tcp/127.0.0.1/6333: Directory nonexistent

그러면 **컨테이너는 멀쩡히 떠 있는데 영원히 unhealthy** 로 남고,
`condition: service_healthy` 로 매달린 블록이 통째로 기동에 실패한다.
qdrant 가 실제로 그 상태였고, 맥에서 `make up` 을 돌리다 드러났다:

    dependency failed to start: container visionai-qdrant-1 is unhealthy

실패가 늦고 원인이 안 보인다 — 로그에는 "unhealthy" 만 남고, 헬스체크 문법이
문제라는 사실은 어디에도 안 적힌다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"


def _healthchecks() -> dict[str, list[str]]:
    document = yaml.safe_load(COMPOSE.read_text("utf-8"))
    found: dict[str, list[str]] = {}
    for name, service in (document.get("services") or {}).items():
        test = (service.get("healthcheck") or {}).get("test")
        if isinstance(test, list):
            found[name] = [str(part) for part in test]
    return found


def test_헬스체크를_찾았다() -> None:
    """파싱이 낡으면 아래 대조가 조용히 통과한다."""
    checks = _healthchecks()

    assert "redis" in checks and "qdrant" in checks, sorted(checks)


@pytest.mark.parametrize("service", sorted(_healthchecks()))
def test_bash_문법은_bash_로_부른다(service: str) -> None:
    """`CMD-SHELL` 은 sh 다. dash·busybox 에서 /dev/tcp 는 없는 문법이다."""
    test = _healthchecks()[service]
    joined = " ".join(test)
    if "/dev/tcp" not in joined:
        pytest.skip("bash 전용 문법을 쓰지 않는다")

    assert test[0] == "CMD", (
        f"{service}: /dev/tcp 를 쓰면서 CMD-SHELL(=sh -c) 로 부른다 — "
        'CMD 로 bash 를 직접 지정한다: ["CMD", "bash", "-c", "..."]'
    )
    assert "bash" in test[1], f"{service}: /dev/tcp 는 bash 에서만 된다 (받은 값: {test[1]})"


@pytest.mark.parametrize("service", sorted(_healthchecks()))
def test_의존하는_쪽이_있으면_헬스체크가_통과할_수_있어야_한다(service: str) -> None:
    """`condition: service_healthy` 로 매달린 블록이 있으면, 그 헬스체크가
    실패하는 순간 **그 블록들이 전부 기동에 실패한다.** 영향 범위가 넓으므로
    문법 실수를 특히 조심해야 하는 자리다."""
    document = yaml.safe_load(COMPOSE.read_text("utf-8"))
    dependents = [
        name
        for name, other in (document.get("services") or {}).items()
        if (other.get("depends_on") or {}).get(service, {}).get("condition") == "service_healthy"
    ]
    if not dependents:
        pytest.skip("이 서비스의 헬스체크에 매달린 블록이 없다")

    test = _healthchecks()[service]

    assert test[0] in {"CMD", "CMD-SHELL"}, f"{service}: 헬스체크 형식이 낯설다 ({test[0]})"
    assert len(test) > 1, f"{service}: 헬스체크 명령이 비었다"
