"""반입 번들의 compose 가 **적재된 이미지로만** 돌 수 있는가.

에어갭 설치는 되돌리기 어렵다. USB 로 들고 들어간 뒤 실패하면 반출입 승인을
다시 밟아야 하고, 고객사에 따라 며칠이 걸린다. 그래서 번들이 스스로 성립하는지를
정적으로 확인한다.

실제로 두 가지가 깨져 있었고 설치 시연에서 처음 드러났다:

1. 개발용 compose 를 그대로 넣어서, 이미지를 다 적재해 놓고도
   `docker compose up` 이 **빌드를 시도**하다 컨텍스트가 없어 죽었다
   (`resolve : lstat /tmp/deploy: no such file or directory`).
2. 엔진이 선택 의존성인 블록(STT-CORE, SPK-DIA)을 `EXTRAS` 없이 빌드해서,
   이미지에 엔진이 없는 채로 반입됐다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"
BUILDER = ROOT / "deploy" / "airgap" / "build_bundle.sh"

import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "deploy" / "airgap"))
from pin_compose import pin, service_image  # noqa: E402

IMAGES = [
    "visionai/ui-meet:0.2.0",
    "visionai/stt-core:0.2.0",
    "visionai/spk-dia:0.2.0",
    "visionai/core-bus:0.2.0",
    "redis:7-alpine",
]


def pinned() -> dict:
    document = yaml.safe_load(COMPOSE.read_text("utf-8"))
    result, _ = pin(document, images=list(IMAGES))
    return result


def test_고정한_compose_에는_build_가_없다() -> None:
    """번들에는 소스가 없다. build: 가 남으면 기동 자체가 실패한다."""
    for name, service in pinned()["services"].items():
        assert "build" not in service, f"{name}: build: 가 남았다"


def test_모든_서비스가_번들에_든_이미지를_가리킨다() -> None:
    for name, service in pinned()["services"].items():
        assert service.get("image") in IMAGES, f"{name}: 번들에 없는 이미지"


def test_밖으로_받으러_나가지_않는다() -> None:
    """폐쇄망에서 pull 시도는 타임아웃으로만 나타나고, 원인이 이미지 누락이라는
    사실은 로그에 안 적힌다."""
    for name, service in pinned()["services"].items():
        assert service.get("pull_policy") == "never", f"{name}: pull_policy 가 없다"


def test_번들에_없는_블록은_빠진다() -> None:
    """남겨 두면 `docker compose up` 이 통째로 실패한다 — 산 블록만 들어간다."""
    services = pinned()["services"]

    assert "ta-assist" not in services
    assert "ui-meet" in services


def test_없어진_서비스를_가리키는_의존이_남지_않는다() -> None:
    """`undefined service` 로 기동 전체가 거부된다."""
    services = pinned()["services"]
    for name, service in services.items():
        for other in service.get("depends_on") or {}:
            assert other in services, f"{name}: 없는 서비스에 의존한다 ({other})"


def test_이미지를_태그가_아니라_저장소로_찾는다() -> None:
    """블록마다 자기 버전을 갖는다. 번들 태그로 맞추면 대부분 어긋나고,
    산 블록이 통째로 빠진 번들이 조용히 나간다."""
    assert service_image("ui-meet", IMAGES) == "visionai/ui-meet:0.2.0"
    assert service_image("없는블록", IMAGES) is None


# ── 엔진이 선택 의존성인 블록 ────────────────────────────────────────────────


def _compose_extras() -> dict[str, str]:
    """개발용 compose 가 각 블록에 주는 EXTRAS 기본값.

    YAML 로 읽는다. 정규식으로 훑으면 한 서비스의 ``BLOCK`` 이 다른 서비스의
    ``EXTRAS`` 와 짝지어져 조용히 틀린 답이 나온다(실제로 그랬다).
    """
    document = yaml.safe_load(COMPOSE.read_text("utf-8"))
    found: dict[str, str] = {}
    for service in (document.get("services") or {}).values():
        args = (service.get("build") or {}).get("args") or {}
        block, extras = args.get("BLOCK"), args.get("EXTRAS")
        if not block or not extras:
            continue
        # ``${VAI_STT_EXTRAS-whisper}`` 에서 기본값만 꺼낸다.
        match = re.fullmatch(r"\$\{[A-Z_]+[:-]-?([a-z]*)\}", str(extras))
        if match and match.group(1):
            found[block] = match.group(1)
    return found


def test_엔진이_필요한_블록을_찾았다() -> None:
    """정규식이 낡으면 아래 대조가 조용히 통과한다."""
    assert _compose_extras() == {"stt-core": "whisper", "spk-dia": "neural"}


@pytest.mark.parametrize("block,extras", sorted(_compose_extras().items()))
def test_번들_빌드도_같은_엔진을_담는다(block: str, extras: str) -> None:
    """두 곳이 갈라지면 개발에서는 되고 **납품본만** 죽는다."""
    script = BUILDER.read_text("utf-8")

    assert re.search(rf"{re.escape(block)}\)\s*echo \"{re.escape(extras)}\"", script), (
        f"{block}: 번들 빌드가 EXTRAS={extras} 를 주지 않는다 — 이미지에 엔진이 없는 채로 반입된다"
    )
