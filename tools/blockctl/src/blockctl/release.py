"""릴리스 빌드 검증 — 납품 직전의 마지막 관문.

개발 체크아웃과 릴리스 빌드의 차이는 딱 하나, **공개키가 내장돼 있는가**다.
그 하나가 빠지면 서명 검증이 꺼진 채로 나가고, 라이선스는 장식이 된다.
그런데 이 상태는 겉으로 완전히 정상으로 보인다 — 블록은 뜨고 상담은 돌고
아무 오류도 없다. 그래서 사람이 눈으로 확인할 수 없고 기계가 막아야 한다.

더 나쁜 실패도 있다. **개인키가 섞여 나가면** 고객사가 무제한 라이선스를 스스로
발급할 수 있고, 이미 나간 모든 라이선스가 무의미해진다. 키를 교체하려면 전
고객사에 새 빌드를 재반입해야 하는데 폐쇄망에서는 사실상 불가능하다.
한 번 새면 되돌릴 수 없으므로 여기서 막는다.

검사는 **빌드 산출물 트리**를 대상으로 한다. 소스 레포가 아니라 실제로 반출될
디렉토리를 봐야 의미가 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PUBLIC_KEY_RELPATH = Path("libs/common/src/vai_common/keys/license_pub.pem")
"""릴리스 빌드가 공개키를 심는 자리. ``vai_common.license.EMBEDDED_PUBLIC_KEY``와
같은 경로여야 한다 — 갈라지면 심었는데 안 읽히는 상태가 된다."""

PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
"""**넣으려는 파일**을 거를 때 쓴다. 여기서는 과하게 거부해도 손해가 없다 —
공개키를 넣으려던 사람이 한 번 더 확인할 뿐이다."""

PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |ENCRYPTED |OPENSSH )?PRIVATE KEY-----"
    rb"[\r\n]+[A-Za-z0-9+/=\s]{64,}"
    rb"-----END (?:RSA |EC |DSA |ENCRYPTED |OPENSSH )?PRIVATE KEY-----"
)
"""**트리를 훑을 때** 쓴다. BEGIN/END 쌍과 본문까지 갖춰야 진짜 키로 본다.

BEGIN 문자열만 찾으면 이 파일 자신처럼 패턴을 상수로 가진 소스가 걸린다.
보안 게이트의 오탐은 단순한 불편이 아니다 — 사람이 경고를 무시하게 만들고,
그렇게 길든 눈은 진짜 유출도 지나친다."""

FORBIDDEN_PACKAGES = ("licgen",)
"""고객사에 가면 안 되는 패키지. 발급기가 함께 나가면 개인키만 구하면
라이선스를 찍어낼 수 있는 상태가 된다."""

SECRET_FILENAMES = (".env", "id_rsa", "id_ed25519", "master.key")

MIN_KEY_BITS = 4096
SCAN_SUFFIXES = {".py", ".pem", ".key", ".json", ".yaml", ".yml", ".env", ".txt", ".sh", ".toml"}
MAX_SCAN_BYTES = 4 * 1024 * 1024
"""이보다 큰 파일은 개인키 검사 대상에서 뺀다 — 모델 가중치·이미지 tar다."""


class Severity(StrEnum):
    BLOCK = "block"
    """납품하면 안 된다. 되돌릴 수 없는 사고로 이어진다."""

    WARN = "warn"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    message: str
    path: str = ""

    def __str__(self) -> str:
        where = f" ({self.path})" if self.path else ""
        return f"[{self.severity.value}] {self.message}{where}"


def embed_public_key(root: Path, key_path: Path) -> Path:
    """공개키를 릴리스 트리에 심는다.

    **개인키를 넣으려 하면 거부한다.** 파일명을 헷갈리는 것은 흔한 실수이고,
    그 실수 한 번의 결과는 되돌릴 수 없다.
    """
    material = key_path.read_bytes()
    if any(marker in material for marker in PRIVATE_KEY_MARKERS):
        raise ValueError(
            f"개인키를 심으려 하고 있다: {key_path} — 공개키(license_pub.pem)를 지정한다"
        )
    if b"-----BEGIN PUBLIC KEY-----" not in material:
        raise ValueError(f"PEM 공개키가 아니다: {key_path}")

    target = root / PUBLIC_KEY_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(material)
    return target


def _check_public_key(root: Path) -> list[Finding]:
    target = root / PUBLIC_KEY_RELPATH
    if not target.is_file():
        return [
            Finding(
                Severity.BLOCK,
                "공개키가 내장되지 않았다 — 서명 검증이 꺼진 채로 납품된다",
                str(PUBLIC_KEY_RELPATH),
            )
        ]

    material = target.read_bytes()
    if PRIVATE_KEY_BLOCK.search(material):
        return [Finding(Severity.BLOCK, "내장된 것이 개인키다", str(PUBLIC_KEY_RELPATH))]

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:  # pragma: no cover - 빌드 환경에는 항상 있다
        return [Finding(Severity.WARN, "cryptography가 없어 공개키를 검증하지 못했다")]

    try:
        key = serialization.load_pem_public_key(material)
    except ValueError as exc:
        return [Finding(Severity.BLOCK, f"공개키를 읽을 수 없다: {exc}", str(PUBLIC_KEY_RELPATH))]

    if not isinstance(key, rsa.RSAPublicKey):
        return [Finding(Severity.BLOCK, "RSA 공개키가 아니다", str(PUBLIC_KEY_RELPATH))]
    if key.key_size < MIN_KEY_BITS:
        return [
            Finding(
                Severity.BLOCK,
                f"RSA-{MIN_KEY_BITS} 미만이다 (현재 {key.key_size}) — 조달 보안 요구를 못 맞춘다",
                str(PUBLIC_KEY_RELPATH),
            )
        ]
    return []


def _iter_files(root: Path) -> list[Path]:
    skip = {"__pycache__", ".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}
    return [
        path for path in sorted(root.rglob("*")) if path.is_file() and not (skip & set(path.parts))
    ]


def _check_no_private_keys(root: Path, files: list[Path]) -> list[Finding]:
    """개인키 유출 검사. 한 번 새면 되돌릴 수 없다."""
    findings: list[Finding] = []
    for path in files:
        if path.suffix and path.suffix not in SCAN_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        if PRIVATE_KEY_BLOCK.search(blob):
            findings.append(
                Finding(
                    Severity.BLOCK,
                    "개인키가 릴리스에 포함돼 있다 — 고객사가 무제한 라이선스를 발급할 수 있다",
                    str(path.relative_to(root)),
                )
            )
    return findings


def _check_no_issuer(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for package in FORBIDDEN_PACKAGES:
        for candidate in (root / "tools" / package, root / package):
            if candidate.exists():
                findings.append(
                    Finding(
                        Severity.BLOCK,
                        f"발급기 '{package}'가 릴리스에 포함돼 있다 — 공급사 전용이다",
                        str(candidate.relative_to(root)),
                    )
                )
    return findings


def _check_no_dev_artifacts(root: Path, files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        relative = path.relative_to(root)
        if path.name in SECRET_FILENAMES:
            findings.append(
                Finding(Severity.BLOCK, f"비밀 파일이 포함돼 있다: {path.name}", str(relative))
            )
        elif "tests" in relative.parts:
            # 테스트에는 고정 비밀키·샘플 라이선스가 들어 있다. 유출은 아니지만
            # 반입 검토 대상만 늘리고 고객사에 혼란을 준다.
            findings.append(Finding(Severity.WARN, "테스트 파일이 포함돼 있다", str(relative)))
    return findings


VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


def _check_version_consistency(root: Path) -> list[Finding]:
    """블록 매니페스트와 패키지 버전이 어긋나면 어느 것이 배포됐는지 알 수 없다."""
    findings: list[Finding] = []
    for manifest in sorted((root / "blocks").glob("*/block.yaml")):
        import yaml

        declared = str(yaml.safe_load(manifest.read_text(encoding="utf-8")).get("version", ""))
        pyproject = manifest.parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        found = VERSION_PATTERN.search(pyproject.read_text(encoding="utf-8"))
        if found and found.group(1) != declared:
            findings.append(
                Finding(
                    Severity.BLOCK,
                    f"버전 불일치: block.yaml={declared} ≠ pyproject={found.group(1)}",
                    str(manifest.parent.relative_to(root)),
                )
            )
    return findings


def verify_release(root: Path) -> list[Finding]:
    """릴리스 트리를 검사한다. ``BLOCK``이 하나라도 있으면 납품하지 않는다."""
    files = _iter_files(root)
    return [
        *_check_public_key(root),
        *_check_no_private_keys(root, files),
        *_check_no_issuer(root),
        *_check_no_dev_artifacts(root, files),
        *_check_version_consistency(root),
    ]


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity is Severity.BLOCK]
