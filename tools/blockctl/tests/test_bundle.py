"""에어갭 반입 계획.

폐쇄망 반입은 되돌릴 수 없다. 이미지 하나가 빠진 채로 USB를 들고 들어가면
반출입 승인을 다시 밟아야 하고, 고객사에 따라 며칠이 걸린다. 그래서 여기서
틀리는 비용이 유난히 크다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blockctl.bundle import INFRA_IMAGES, checksum_manifest, licensed_blocks, plan
from blockctl.catalog import CatalogError, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def catalog() -> list:
    return load_catalog(REPO_ROOT / "blocks")


# --- 의존 폐포 -----------------------------------------------------------


def test_의존의_의존까지_따라온다(catalog: list) -> None:
    """직접 의존은 눈에 보이지만 의존의 의존은 안 보인다. 손으로 세면 여기서 빠진다."""
    result = plan(catalog, ["UI-AGENT"])
    # UI-AGENT → SCN-STUDIO → RAG-SRCH 로 이어지는 사슬이 전부 담겨야 한다.
    assert {"UI-AGENT", "SCN-STUDIO", "RAG-SRCH", "CORE-BUS"} <= set(result.blocks)


def test_의존으로_추가된_블록을_따로_보고한다(catalog: list) -> None:
    """조용히 늘어나면 반입 승인 목록과 실제 번들이 어긋난다."""
    result = plan(catalog, ["UI-AGENT"])
    assert "UI-AGENT" not in result.added_by_dependency  # 요청한 것은 빠진다
    assert "CORE-BUS" in result.added_by_dependency


def test_기능_의존은_담되_따로_보고한다(catalog: list) -> None:
    """UI-AGENT는 TA-ASSIST 없이도 뜬다 — 자막과 경고는 나온다. 다만 지식 팝업
    패널이 영원히 비어 있다. 담지 않으면 '패널이 죽은 채로' 납품되고,
    조용히 담으면 라이선스 범위를 넘는 반입이 영업과 대조되지 않는다."""
    result = plan(catalog, ["UI-AGENT"])
    assert "TA-ASSIST" in result.blocks
    assert result.added_by_recommendation == ["TA-ASSIST"]


def test_요청한_블록은_추천_목록에_넣지_않는다(catalog: list) -> None:
    """이미 라이선스에 있는 블록을 '범위 밖 반입'으로 보고하면 오탐이다."""
    result = plan(catalog, ["UI-AGENT", "TA-ASSIST"])
    assert result.added_by_recommendation == []


def test_소비하는_토픽의_생산자가_함께_담긴다(catalog: list) -> None:
    """이벤트로만 이어진 블록은 depends_on에 안 나타난다. 이것 없이는
    '소비자만 있고 생산자가 없는' 번들이 된다 — 기동은 되고 아무 일도 안 일어난다."""
    result = plan(catalog, ["UI-MEET"])
    # UI-MEET은 speaker.label을 소비한다. 생산자는 SPK-DIA다.
    assert "SPK-DIA" in result.blocks


def test_CORE_LIC은_라이선스에_없어도_담는다(catalog: list) -> None:
    """라이선스를 설치하는 블록이 번들에 없으면 최초 구축에서 아무것도 기동할 수 없다."""
    result = plan(catalog, ["CORE-BUS"])
    assert "CORE-LIC" in result.blocks


def test_카탈로그에_없는_블록은_거부한다(catalog: list) -> None:
    """라이선스 오타이거나 미출시 블록이다. 번들을 만들고 나서 알면 늦다."""
    with pytest.raises(CatalogError, match="NOT-A-BLOCK"):
        plan(catalog, ["NOT-A-BLOCK"])


def test_중복_요청은_한_번만_담는다(catalog: list) -> None:
    once = plan(catalog, ["CORE-BUS"])
    twice = plan(catalog, ["CORE-BUS", "CORE-BUS"])
    assert once.blocks == twice.blocks


# --- 인프라 -------------------------------------------------------------


def test_검색_블록은_qdrant를_끌고_온다(catalog: list) -> None:
    """RAG-SRCH만 반입하고 qdrant를 빼면 검색이 통째로 안 된다."""
    result = plan(catalog, ["RAG-SRCH"])
    assert "qdrant" in result.infra
    assert INFRA_IMAGES["qdrant"] in result.images


def test_버스를_쓰는_블록은_redis를_끌고_온다(catalog: list) -> None:
    result = plan(catalog, ["CORE-BUS"])
    assert INFRA_IMAGES["redis"] in result.images


def test_인프라_이미지_태그가_compose와_같다() -> None:
    """갈라지면 번들로 설치한 환경과 개발 환경의 버전이 달라지고,
    재현되지 않는 장애가 생긴다."""
    compose = (REPO_ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    for image in INFRA_IMAGES.values():
        assert image in compose, f"compose에 없는 태그: {image}"


def test_모든_블록_이미지가_계획에_담긴다(catalog: list) -> None:
    result = plan(catalog, [b.manifest.id for b in catalog])
    assert len(result.blocks) == len(catalog)
    for block in catalog:
        assert f"visionai/{block.directory}:{block.manifest.version}" in result.images


# --- 라이선스 연동 -------------------------------------------------------


def test_라이선스에서_허가_블록만_읽는다(tmp_path: Path) -> None:
    """enabled=false 블록까지 담으면 팔지 않은 기능이 고객사에 반입된다."""
    path = tmp_path / "customer.lic"
    path.write_text(
        json.dumps(
            {
                "payload": {
                    "blocks": {
                        "STT-CORE": {"enabled": True, "concurrent_channels": 50},
                        "RAG-SRCH": {"enabled": True},
                        "AVA-COUNSEL": {"enabled": False},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert licensed_blocks(path) == ["RAG-SRCH", "STT-CORE"]


def test_서명_없는_라이선스도_읽는다(tmp_path: Path) -> None:
    """번들 빌드는 공급사 내부 작업이고 검증은 고객사 서버에서 각 블록이 한다.
    여기서 공개키를 요구하면 빌드 서버에 검증 자산만 하나 더 는다."""
    path = tmp_path / "plain.lic"
    path.write_text(json.dumps({"blocks": {"CORE-BUS": {"enabled": True}}}), encoding="utf-8")
    assert licensed_blocks(path) == ["CORE-BUS"]


def test_라이선스로_계획을_세운다(catalog: list, tmp_path: Path) -> None:
    path = tmp_path / "customer.lic"
    path.write_text(
        json.dumps({"payload": {"blocks": {"UI-MEET": {"enabled": True}}}}), encoding="utf-8"
    )
    result = plan(catalog, licensed_blocks(path))
    # 회의록 패키지: 화면만 사도 화자분리·요약·버스가 따라와야 쓸 수 있다.
    assert {"UI-MEET", "SPK-DIA", "LLM-SUM", "CORE-BUS", "CORE-LIC"} <= set(result.blocks)


# --- 체크섬 -------------------------------------------------------------


def test_체크섬은_모든_파일을_담고_자기_자신은_뺀다(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "images.tar").write_bytes(b"payload")
    (tmp_path / "sub" / "plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("stale", encoding="utf-8")

    digests = checksum_manifest(tmp_path)
    assert set(digests) == {"images.tar", "sub/plan.json"}
    assert all(len(v) == 64 for v in digests.values())


def test_내용이_바뀌면_체크섬이_바뀐다(tmp_path: Path) -> None:
    target = tmp_path / "images.tar"
    target.write_bytes(b"original")
    before = checksum_manifest(tmp_path)["images.tar"]
    target.write_bytes(b"tampered")
    assert checksum_manifest(tmp_path)["images.tar"] != before


def test_생산자가_여럿인_토픽은_전부_담지_않는다(catalog: list) -> None:
    """audit.log의 생산자는 넷이다. CORE-SEC은 그중 하나만 있어도 제 일을 한다.
    전부 담으면 라이선스 범위 밖 이미지가 무더기로 들어간다."""
    result = plan(catalog, ["CORE-SEC"])
    assert set(result.blocks) == {"CORE-SEC", "CORE-BUS", "CORE-LIC"}


def test_생산자가_하나도_없으면_경고한다(catalog: list, tmp_path: Path) -> None:
    """'소비자만 있고 생산자가 없는' 번들은 기동은 되고 아무 일도 일어나지 않는다.
    조용히 넘어가면 고객사가 왜 안 되는지 몰라 공급사 전화로 이어진다."""
    manifest = tmp_path / "solo" / "block.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "id: SOLO\nname: 고아 소비자\ntier: intelligence\nversion: 0.1.0\n"
        "contracts:\n  consumes:\n    - nobody.produces.this\n  produces: []\n"
        "billing:\n  capacity_unit: none\n  included_in_package: true\n",
        encoding="utf-8",
    )
    result = plan(load_catalog(tmp_path), ["SOLO"])
    assert any("생산자가 카탈로그에 없다" in w for w in result.warnings)
