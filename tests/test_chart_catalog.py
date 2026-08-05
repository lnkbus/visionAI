"""Helm 차트와 카탈로그의 정합성.

블록을 추가하고 차트에 넣는 것을 잊으면 **배포할 수 없는 블록**이 생긴다.
반대로 차트에만 있고 카탈로그에 없으면 이미지가 없어 기동에 실패한다.
둘 다 K8s 배포 당일에야 드러나고, 폐쇄망에서는 그날이 검수일이다.

값 파일(패키지 오버레이)도 함께 검사한다. 오버레이에 오타가 있으면 그 블록은
**조용히 기본값을 따라** 켜지거나 꺼진다 — 계약한 패키지와 다른 것이 배포된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "deploy" / "charts" / "visionai"
PACKAGE_VALUES = ("values-aicc.yaml", "values-meeting.yaml", "values-voicebot.yaml")


def _catalog_dirs() -> set[str]:
    return {path.parent.name for path in (REPO_ROOT / "blocks").glob("*/block.yaml")}


def _values(name: str = "values.yaml") -> dict:
    return yaml.safe_load((CHART_DIR / name).read_text(encoding="utf-8"))


def test_차트에_모든_블록이_있다() -> None:
    """블록을 추가하고 차트를 잊으면 배포할 수 없는 블록이 생긴다."""
    missing = sorted(_catalog_dirs() - set(_values()["blocks"]))
    assert not missing, f"차트 values.yaml에 없는 블록: {missing}"


def test_차트에_없는_블록이_없다() -> None:
    """카탈로그에 없는 블록은 이미지가 없어 기동에 실패한다."""
    extra = sorted(set(_values()["blocks"]) - _catalog_dirs())
    assert not extra, f"카탈로그에 없는 블록이 차트에 있다: {extra}"


def test_포트가_중복되지_않는다() -> None:
    """같은 포트를 두 블록이 쓰면 단일 노드 구성(K3s·compose)에서 충돌한다."""
    ports: dict[int, str] = {}
    for name, block in _values()["blocks"].items():
        port = block["port"]
        assert port not in ports, f"포트 {port} 중복: {ports[port]} ↔ {name}"
        ports[port] = name


def test_포트가_소스와_일치한다() -> None:
    """차트의 포트와 블록이 실제로 여는 포트가 다르면 헬스체크가 영원히 실패한다."""
    import re

    for name, block in _values()["blocks"].items():
        module = name.replace("-", "_")
        main = REPO_ROOT / "blocks" / name / "src" / f"vai_{module}" / "main.py"
        if not main.is_file():
            continue
        found = re.search(r"default_port=(\d+)", main.read_text(encoding="utf-8"))
        assert found, f"{name}: main.py에서 default_port를 찾지 못했다"
        assert int(found.group(1)) == block["port"], (
            f"{name}: 차트 {block['port']} ≠ 소스 {found.group(1)}"
        )


@pytest.mark.parametrize("values_file", PACKAGE_VALUES)
def test_패키지_오버레이에_오타가_없다(values_file: str) -> None:
    """오버레이의 블록 이름이 틀리면 그 블록은 조용히 기본값을 따른다 —
    계약한 패키지와 다른 것이 배포되고, 아무도 즉시 알아채지 못한다."""
    unknown = sorted(set(_values(values_file)["blocks"]) - _catalog_dirs())
    assert not unknown, f"{values_file}에 알 수 없는 블록: {unknown}"


@pytest.mark.parametrize("values_file", PACKAGE_VALUES)
def test_패키지가_모든_블록을_명시한다(values_file: str) -> None:
    """켤지 끌지를 적지 않은 블록은 기본값(전부 켜짐)을 따라간다.
    회의록 패키지에 상담 지원 블록이 딸려 들어가면 GPU·메모리를 그냥 먹는다."""
    declared = set(_values(values_file)["blocks"])
    missing = sorted(_catalog_dirs() - declared)
    assert not missing, f"{values_file}에 명시되지 않은 블록: {missing}"


@pytest.mark.parametrize("values_file", PACKAGE_VALUES)
def test_패키지에_기반_블록이_포함된다(values_file: str) -> None:
    """CORE-LIC이 빠지면 라이선스를 설치할 창구가 없고, CORE-BUS가 빠지면
    아무 이벤트도 흐르지 않는다."""
    blocks = _values(values_file)["blocks"]
    for required in ("core-bus", "core-lic", "core-sec"):
        assert blocks[required]["enabled"], f"{values_file}: {required}가 꺼져 있다"


def test_단일_라이터_블록은_복제하지_않는다() -> None:
    """CORE-SEC은 해시 체인이, CORE-LIC은 라이선스 파일이 깨진다.
    템플릿이 강제하지만 values에서도 1로 두어 의도를 드러낸다."""
    blocks = _values()["blocks"]
    for name in ("core-sec", "core-lic"):
        assert blocks[name].get("replicas", 1) == 1, f"{name}: replicas가 1이 아니다"


def test_에어갭에서_이미지를_당겨오지_않는다() -> None:
    """폐쇄망에는 레지스트리가 없다. Always면 전 블록이 기동에 실패한다."""
    assert _values()["image"]["pullPolicy"] == "IfNotPresent"


def test_라이선스와_감사_볼륨은_릴리스와_함께_지워지지_않는다() -> None:
    """라이선스가 지워지면 발급 절차를 처음부터 다시 밟아야 하고(폐쇄망에서 며칠),
    감사 로그가 지워지는 것은 그 자체로 감사 사고다."""
    storage = (CHART_DIR / "templates" / "storage.yaml").read_text(encoding="utf-8")
    assert storage.count("helm.sh/resource-policy: keep") == 2


# --- 렌더링 (helm이 있을 때만) --------------------------------------------


def _helm(*args: str) -> str:
    import shutil
    import subprocess

    if shutil.which("helm") is None:
        pytest.skip("helm이 없다")
    result = subprocess.run(
        ["helm", *args], cwd=CHART_DIR, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _deployments(rendered: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == "Deployment"
    }


@pytest.mark.parametrize("values_file", ("", *PACKAGE_VALUES))
def test_차트가_렌더링된다(values_file: str) -> None:
    """오버레이 하나가 깨지면 그 패키지는 배포 당일에 막힌다."""
    args = ["template", "visionai", "."]
    if values_file:
        args += ["-f", values_file]
    assert "kind: Deployment" in _helm(*args)


def test_replicas를_올려도_단일_라이터는_하나다() -> None:
    """values에서 실수로 올릴 수 있다. '성능을 올리려다 감사 로그를 못 쓰게
    만드는' 사고를 설정 실수로 낼 수 있게 두지 않는다."""
    rendered = _helm(
        "template",
        "visionai",
        ".",
        "--set",
        "blocks.core-sec.replicas=5",
        "--set",
        "blocks.core-lic.replicas=3",
    )
    deployments = _deployments(rendered)
    assert deployments["visionai-core-sec"]["spec"]["replicas"] == 1
    assert deployments["visionai-core-lic"]["spec"]["replicas"] == 1
    # 롤링 업데이트로 두 파드가 겹치는 순간에도 같은 파일에 쓰면 안 된다.
    assert deployments["visionai-core-sec"]["spec"]["strategy"]["type"] == "Recreate"


def test_일반_블록은_복제된다() -> None:
    """단일 라이터 규칙이 과하게 적용되면 확장이 통째로 막힌다."""
    deployments = _deployments(
        _helm("template", "visionai", ".", "--set", "blocks.core-bus.replicas=4")
    )
    assert deployments["visionai-core-bus"]["spec"]["replicas"] == 4


def test_CORE_LIC만_라이선스를_쓰기_가능하게_붙인다() -> None:
    """다른 블록이 자기 라이선스를 고쳐 쓸 수 있으면 DRM이 무의미해진다."""
    writable = set()
    for name, doc in _deployments(_helm("template", "visionai", ".")).items():
        for mount in doc["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
            if mount["name"] == "license" and not mount.get("readOnly", False):
                writable.add(name.replace("visionai-", ""))
    assert writable == {"core-lic"}


def test_회의록_패키지에_상담_지원_블록이_없다() -> None:
    """켜 두면 쓰지도 않는 GPU·메모리를 그냥 먹는다."""
    names = set(_deployments(_helm("template", "visionai", ".", "-f", "values-meeting.yaml")))
    for absent in ("visionai-ta-assist", "visionai-aud-rtp", "visionai-ui-agent"):
        assert absent not in names
    assert "visionai-spk-dia" in names and "visionai-ui-meet" in names
