"""compose 가 주는 환경변수를 블록이 실제로 읽는가.

pydantic-settings 는 **모르는 변수를 조용히 무시한다**(``extra="ignore"``).
그래서 접두사를 한 글자 틀리면 오류도 경고도 없이 기본값으로 돌고, 그 사실은
기능이 안 되는 모양으로만 나타난다 — "신경망 임베더를 켰는데 비슷한 목소리를
못 가른다" 같은 증상이고, 원인을 찾는 데 하루가 든다.

실제로 그런 결함이 있었다: SPK-DIA 는 ``VAI_DIA_*`` 를 읽는데 compose 는
``VAI_SPK_*`` 로 줬다. 화면도 로그도 아무 말을 하지 않았다.

그래서 이름을 대조한다. compose 의 모든 ``VAI_*`` 변수는 **어느 블록의 설정
필드이거나 공통 설정**이어야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"

_PREFIX = re.compile(r'env_prefix="(VAI_[A-Z_]*)"')
_FIELD = re.compile(r"^    ([a-z][a-z0-9_]*)\s*:\s*[^=]+=", re.M)

ALLOWED_EXTRA = {
    # compose 가 컨테이너에 넘기지만 설정 클래스가 아니라 다른 곳이 읽는 것들.
    "VAI_MODEL_DIR",  # 볼륨 경로 치환용 — 컨테이너 안으로 들어가지 않는다
    "VAI_SPK_EXTRAS",  # 이미지 빌드 인자
}


def _settings_map() -> dict[str, set[str]]:
    """접두사 → 그 접두사로 읽히는 필드 이름들."""
    found: dict[str, set[str]] = {}
    for path in [*(ROOT / "blocks").rglob("*.py"), *(ROOT / "libs").rglob("*.py")]:
        text = path.read_text("utf-8")
        prefixes = _PREFIX.findall(text)
        if not prefixes:
            continue
        # 한 파일에 설정 클래스가 여럿이면 필드를 모두 합쳐 본다. 느슨하지만
        # 이 시험이 잡으려는 것은 "아무 데도 없는 이름"이다.
        fields = {name.upper() for name in _FIELD.findall(text)}
        for prefix in prefixes:
            found.setdefault(prefix, set()).update(fields)
    return found


def _compose_env() -> dict[str, set[str]]:
    document = yaml.safe_load(COMPOSE.read_text("utf-8"))
    services: dict[str, set[str]] = {}
    for name, service in (document.get("services") or {}).items():
        env = service.get("environment") or {}
        keys = set(env) if isinstance(env, dict) else {item.split("=", 1)[0] for item in env}
        services[name] = {key for key in keys if key.startswith("VAI_")}
    return services


def test_설정_접두사를_찾을_수_있다() -> None:
    """정규식이 낡으면 이 시험 전체가 조용히 통과한다."""
    prefixes = _settings_map()

    assert "VAI_DIA_" in prefixes
    assert "VAI_STT_" in prefixes
    assert "adapter".upper() in prefixes["VAI_STT_"]


@pytest.mark.parametrize("service", sorted(_compose_env()))
def test_compose_환경변수를_블록이_읽는다(service: str) -> None:
    """읽히지 않는 변수는 설정이 아니라 **주석**이다.

    pydantic-settings 가 조용히 무시하므로, 이름이 틀렸다는 사실이 어디에도
    드러나지 않는다.
    """
    settings = _settings_map()
    common = settings.get("VAI_", set())
    unknown: list[str] = []

    for key in sorted(_compose_env()[service]):
        if key in ALLOWED_EXTRA:
            continue
        # 가장 긴 접두사부터 맞춘다 — VAI_ 는 모든 이름에 걸린다.
        matched = False
        for prefix in sorted(settings, key=len, reverse=True):
            if prefix == "VAI_" or not key.startswith(prefix):
                continue
            if key[len(prefix) :] in settings[prefix]:
                matched = True
            break
        if not matched and key[len("VAI_") :] in common:
            matched = True
        if not matched:
            unknown.append(key)

    assert not unknown, (
        f"{service}: 어느 블록도 읽지 않는 변수 {unknown} — "
        "접두사가 맞는지 확인한다(설정 클래스의 env_prefix)"
    )
