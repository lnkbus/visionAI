"""릴리스 검사.

여기서 막지 못하면 고객사 현장에서야 드러나고, 어떤 실패는 되돌릴 수 없다.

- **공개키 누락**: 서명 검증이 꺼진 채로 납품된다. 겉으로는 완전히 정상이라
  사람이 눈으로 확인할 수 없다.
- **개인키 유출**: 고객사가 무제한 라이선스를 발급할 수 있고, 이미 나간 모든
  라이선스가 무의미해진다. 키 교체는 전 고객사 재반입을 뜻해 폐쇄망에서는
  사실상 불가능하다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blockctl.release import (
    PUBLIC_KEY_RELPATH,
    Severity,
    blocking,
    embed_public_key,
    verify_release,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def key_pair() -> tuple[bytes, bytes]:
    from licgen.issuer import generate_key_pair

    return generate_key_pair(4096)


@pytest.fixture
def release_tree(tmp_path: Path, key_pair: tuple[bytes, bytes]) -> Path:
    """통과하는 최소 릴리스 트리."""
    _, public_pem = key_pair
    root = tmp_path / "tree"
    (root / "blocks").mkdir(parents=True)
    embed_public_key(root, _write(tmp_path / "pub.pem", public_pem))
    return root


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _messages(root: Path) -> str:
    return "\n".join(str(f) for f in verify_release(root))


# --- 공개키 --------------------------------------------------------------


def test_공개키가_있으면_통과한다(release_tree: Path) -> None:
    assert blocking(verify_release(release_tree)) == []


def test_공개키가_없으면_납품을_막는다(tmp_path: Path) -> None:
    """이 상태는 겉으로 완전히 정상으로 보인다 — 블록은 뜨고 상담은 돌고
    아무 오류도 없다. 기계가 막지 않으면 아무도 못 잡는다."""
    root = tmp_path / "tree"
    (root / "blocks").mkdir(parents=True)
    findings = blocking(verify_release(root))
    assert len(findings) == 1
    assert "서명 검증이 꺼진" in findings[0].message


def test_개인키를_공개키_자리에_심으려_하면_거부한다(
    tmp_path: Path, key_pair: tuple[bytes, bytes]
) -> None:
    """파일명을 헷갈리는 것은 흔한 실수이고, 그 결과는 되돌릴 수 없다."""
    private_pem, _ = key_pair
    with pytest.raises(ValueError, match="개인키"):
        embed_public_key(tmp_path / "tree", _write(tmp_path / "priv.pem", private_pem))


def test_PEM_공개키가_아니면_거부한다(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PEM 공개키"):
        embed_public_key(tmp_path / "tree", _write(tmp_path / "junk.pem", b"not a key"))


def test_RSA_4096_미만은_납품을_막는다(tmp_path: Path) -> None:
    """RSA-2048로 나가면 조달 보안 요구를 못 맞춘다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    weak = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = weak.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    root = tmp_path / "tree"
    (root / "blocks").mkdir(parents=True)
    _write(root / PUBLIC_KEY_RELPATH, pem)

    findings = blocking(verify_release(root))
    assert any("4096" in f.message for f in findings)


# --- 개인키 유출 ---------------------------------------------------------


def test_개인키가_섞여_있으면_납품을_막는다(
    release_tree: Path, key_pair: tuple[bytes, bytes]
) -> None:
    """한 번 새면 되돌릴 수 없다. 키 교체는 전 고객사 재반입을 뜻한다."""
    private_pem, _ = key_pair
    _write(release_tree / "deploy" / "leftover.pem", private_pem)

    findings = blocking(verify_release(release_tree))
    assert any("개인키가 릴리스에 포함" in f.message for f in findings)


def test_소스에_적힌_패턴_문자열은_오탐하지_않는다(release_tree: Path) -> None:
    """보안 게이트의 오탐은 단순한 불편이 아니다 — 사람이 경고를 무시하게
    만들고, 그렇게 길든 눈은 진짜 유출도 지나친다."""
    _write(
        release_tree / "scanner.py",
        b'MARKERS = ("-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----")\n',
    )
    assert blocking(verify_release(release_tree)) == []


def test_검사기_자신이_통과한다() -> None:
    """이 모듈은 탐지 패턴을 상수로 갖고 있다. 자기 자신에 걸리면
    릴리스 검사가 영원히 실패한다."""
    from blockctl.release import PRIVATE_KEY_BLOCK

    source = (REPO_ROOT / "tools" / "blockctl" / "src" / "blockctl" / "release.py").read_bytes()
    assert PRIVATE_KEY_BLOCK.search(source) is None


def test_비밀_파일이_있으면_막는다(release_tree: Path) -> None:
    _write(release_tree / ".env", b"VAI_ADM_JWT_SECRET=hunter2\n")
    assert any("비밀 파일" in f.message for f in blocking(verify_release(release_tree)))


# --- 공급사 전용 자산 ----------------------------------------------------


def test_발급기가_남아_있으면_막는다(release_tree: Path) -> None:
    """발급기가 함께 나가면 개인키만 구하면 라이선스를 찍어낼 수 있다."""
    (release_tree / "tools" / "licgen").mkdir(parents=True)
    assert any("발급기" in f.message for f in blocking(verify_release(release_tree)))


def test_테스트_파일은_경고에_그친다(release_tree: Path) -> None:
    """유출은 아니지만 반입 검토 대상만 늘리고 고객사에 혼란을 준다.
    납품을 막을 정도는 아니다 — 모든 것을 차단으로 두면 게이트가 무시된다."""
    _write(release_tree / "blocks" / "x" / "tests" / "test_x.py", b"")
    findings = verify_release(release_tree)
    assert blocking(findings) == []
    assert any(f.severity is Severity.WARN for f in findings)


# --- 버전 정합성 ---------------------------------------------------------


def test_버전이_어긋나면_막는다(release_tree: Path) -> None:
    """어느 버전이 배포됐는지 알 수 없으면 장애 재현도 회귀 확인도 못 한다."""
    block = release_tree / "blocks" / "demo"
    block.mkdir(parents=True)
    (block / "block.yaml").write_text(
        "id: DEMO\nname: 데모\ntier: foundation\nversion: 0.2.0\n"
        "billing:\n  capacity_unit: none\n  included_in_package: true\n",
        encoding="utf-8",
    )
    (block / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")

    assert any("버전 불일치" in f.message for f in blocking(verify_release(release_tree)))


def test_레포_자체는_공개키가_없어_막힌다() -> None:
    """개발 체크아웃이 그대로 납품되는 것을 막는다 — 릴리스 빌드를 반드시 거친다."""
    findings = blocking(verify_release(REPO_ROOT))
    assert any("서명 검증이 꺼진" in f.message for f in findings)
    assert any("발급기" in f.message for f in findings)
