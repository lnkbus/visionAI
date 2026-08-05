"""H/W 지문 — 완화 정책의 경계가 여기서 결정된다.

지문이 너무 엄격하면 NIC 교체 한 번에 고객사 서비스가 멈추고, 너무 느슨하면
다른 서버로 복사해도 돈다. 그 경계를 테스트로 고정한다.
"""

from __future__ import annotations

import pytest

from vai_common import fingerprint as fp


def _fingerprint(**components: str) -> fp.Fingerprint:
    return fp.Fingerprint(components=dict(components))


def test_동일한_부품이면_전체_해시가_같다() -> None:
    left = _fingerprint(gpu="g", cpu="c", mac="m")
    right = _fingerprint(mac="m", cpu="c", gpu="g")
    # 딕셔너리 삽입 순서가 달라도 같아야 한다. 안 그러면 재기동마다 지문이 바뀐다.
    assert left.combined == right.combined


def test_부품_하나가_바뀌면_전체_해시가_달라진다() -> None:
    assert _fingerprint(gpu="g", cpu="c").combined != _fingerprint(gpu="g", cpu="c2").combined


def test_부품_하나_교체는_허용한다() -> None:
    """NIC 교체 시나리오. 정상 유지보수로 라이선스가 죽으면 안 된다."""
    installed = _fingerprint(gpu="g", cpu="c", machine="i", mac="old")
    after_nic_swap = _fingerprint(gpu="g", cpu="c", machine="i", mac="new")
    assert after_nic_swap.matches(installed, required=2)


def test_다른_서버로의_복사는_막는다() -> None:
    installed = _fingerprint(gpu="g1", cpu="c1", machine="i1", mac="m1")
    other_server = _fingerprint(gpu="g2", cpu="c2", machine="i2", mac="m2")
    assert not other_server.matches(installed, required=2)


def test_같은_사양_서버라도_한_부품만_겹치면_거부한다() -> None:
    """같은 모델 서버를 여러 대 사면 cpu 해시가 겹친다. 그것만으로는 안 된다."""
    installed = _fingerprint(gpu="g1", cpu="same-model", machine="i1", mac="m1")
    twin = _fingerprint(gpu="g2", cpu="same-model", machine="i2", mac="m2")
    assert not twin.matches(installed, required=2)


def test_부품이_하나뿐이면_그것만_일치해도_통과한다() -> None:
    """제한된 컨테이너처럼 machine-id 하나만 잡히는 환경. required=2를
    문자 그대로 적용하면 무조건 실패해 아무도 기동하지 못한다."""
    recorded = _fingerprint(machine="i1")
    assert _fingerprint(machine="i1").matches(recorded, required=2)
    assert not _fingerprint(machine="i2").matches(recorded, required=2)


def test_지문이_비면_어느_쪽도_일치로_보지_않는다() -> None:
    """빈 지문끼리 combined가 같다고 통과시키면 DRM이 무력화된다."""
    empty = fp.Fingerprint()
    assert not empty.matches(_fingerprint(gpu="g"), required=2)


def test_알_수_없는_부품은_복원_시_버린다() -> None:
    restored = fp.from_components({"gpu": "g", "unknown": "x"})
    assert set(restored.components) == {"gpu"}


def test_describe는_해시_앞부분만_노출한다() -> None:
    described = _fingerprint(gpu="g" * 64).describe()
    assert len(described["gpu"]) == 16
    assert "hash" in described


def test_임의_MAC은_지문에서_제외한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """uuid.getnode()가 만들어 낸 임의 값이 지문에 들어가면 재부팅마다 깨진다."""
    import uuid

    monkeypatch.setattr(uuid, "getnode", lambda: 0x010000000000 | 0xABCDEF)
    assert fp.collect_mac() is None


def test_실제_서버에서_최소_한_부품은_잡힌다() -> None:
    """수집 자체가 도는지 확인한다. 리눅스라면 machine-id 또는 cpuinfo가 있다."""
    assert fp.collect(include_gpu=False).components
