"""H/W Fingerprint 수집 — 사양서 §5의 오프라인 DRM 기반.

설치 서버의 GPU UUID·CPU 식별자·MAC 주소를 모아 SHA-256 해시를 만든다.
라이선스는 이 지문에 묶이므로, 다른 서버에 복사해도 기동하지 않는다.

**부품별 해시를 따로 남기는 이유**: 지문 전체를 하나의 해시로만 두면 NIC 하나를
교체하거나 GPU를 증설하는 순간 라이선스가 죽는다. 고객사 입장에서는 정상적인
유지보수인데 서비스가 멈추고, 공급사는 재발급 지원에 인력을 쓴다.
부품별로 남겨 두면 "N개 이상 일치"라는 완화된 정책을 쓸 수 있다.

수집은 **실패해도 예외를 던지지 않는다.** 폐쇄망 서버에 GPU가 없거나
권한이 부족한 경우가 흔하고, 그 경우 나머지 부품으로 지문을 만든다.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

COMPONENT_ORDER = ("gpu", "cpu", "machine", "mac")
"""지문에 담기는 부품. 순서를 고정해야 같은 서버에서 같은 해시가 나온다."""

DEFAULT_REQUIRED_MATCHES = 2
"""이만큼 일치하면 같은 서버로 본다. 부품 하나 교체는 허용하되 다른 서버로의
복제는 막는 절충점이다."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _run(command: list[str], timeout: float = 3.0) -> str:
    """외부 명령 실행. 실패하면 빈 문자열 — 지문 수집이 기동을 막으면 안 된다."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def collect_gpu() -> str | None:
    """GPU UUID. 폐쇄망 AI 서버의 가장 안정적인 식별자다."""
    output = _run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"])
    uuids = [line.strip() for line in output.splitlines() if line.strip()]
    # 여러 장이면 정렬해 붙인다. GPU 순서가 재부팅마다 바뀔 수 있다.
    return ",".join(sorted(uuids)) if uuids else None


def collect_cpu() -> str | None:
    """CPU 식별자.

    ``/proc/cpuinfo``의 serial(ARM) 또는 모델명+코어수 조합을 쓴다. x86에는
    시리얼이 없는 경우가 많아 모델·코어수로 대체하는데, 같은 사양 서버끼리는
    겹친다 — 그래서 이것만으로 지문을 삼지 않는다.
    """
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return None
    with contextlib.suppress(OSError):
        text = cpuinfo.read_text(encoding="utf-8", errors="ignore")
        serial = re.search(r"^Serial\s*:\s*(\S+)", text, re.MULTILINE)
        if serial:
            return serial.group(1)
        model = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        cores = text.count("processor\t:")
        if model:
            return f"{model.group(1).strip()}|{cores}"
    return None


def collect_machine_id() -> str | None:
    """systemd machine-id. OS 재설치 시 바뀌므로 GPU/MAC보다 약한 신호다."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        candidate = Path(path)
        if candidate.exists():
            with contextlib.suppress(OSError):
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
    return None


def collect_mac() -> str | None:
    """MAC 주소.

    ``uuid.getnode()``는 MAC을 못 구하면 임의 값을 만들어 낸다. 그 경우
    지문이 매번 달라지므로 반드시 걸러야 한다 — 안 그러면 재부팅 때마다
    라이선스가 깨진다.
    """
    node = uuid.getnode()
    if (node >> 40) & 0x01:
        # 멀티캐스트 비트가 서 있으면 파이썬이 만들어 낸 임의 값이다.
        log.warning("MAC 주소를 구하지 못해 지문에서 제외한다")
        return None
    return f"{node:012x}"


@dataclass(frozen=True)
class Fingerprint:
    """서버 지문. 부품별 해시와 전체 해시를 함께 담는다."""

    components: dict[str, str] = field(default_factory=dict)
    """부품명 → SHA-256. 수집에 실패한 부품은 빠진다."""

    @property
    def combined(self) -> str:
        """전체 해시. 순서를 고정해 같은 서버에서 항상 같은 값이 나온다."""
        parts = [
            f"{name}={self.components[name]}" for name in COMPONENT_ORDER if name in self.components
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    @property
    def component_count(self) -> int:
        return len(self.components)

    def matches(self, other: Fingerprint, required: int = DEFAULT_REQUIRED_MATCHES) -> bool:
        """같은 서버인지 판정한다.

        전체 해시가 같으면 즉시 통과. 아니면 부품별로 세어 ``required`` 이상
        일치할 때만 통과한다. 부품 수 자체가 그보다 적으면 전부 일치해야 한다 —
        부품 하나뿐인 서버에서 "2개 일치" 조건이 무조건 실패하는 것을 막는다.
        """
        if self.combined == other.combined:
            return True
        shared = {
            name
            for name in self.components
            if name in other.components and self.components[name] == other.components[name]
        }
        threshold = min(required, len(other.components), len(self.components))
        return threshold > 0 and len(shared) >= threshold

    def describe(self) -> dict[str, str]:
        """발급 요청서에 넣을 요약. 실제 값이 아니라 해시만 나간다."""
        return {"hash": self.combined, **{k: v[:16] for k, v in self.components.items()}}


def collect(*, include_gpu: bool = True) -> Fingerprint:
    """현재 서버의 지문을 수집한다.

    ``include_gpu=False``는 GPU가 교체·증설되는 환경에서 쓴다. 지문이 약해지는
    대신 유지보수 중 라이선스가 깨지지 않는다 — 고객사와 합의할 사항이다.
    """
    raw: dict[str, str | None] = {
        "gpu": collect_gpu() if include_gpu else None,
        "cpu": collect_cpu(),
        "machine": collect_machine_id(),
        "mac": collect_mac(),
    }
    components = {name: _digest(value) for name, value in raw.items() if value}
    if not components:
        # 아무것도 못 구한 환경(제한된 컨테이너 등). 라이선스를 묶을 수 없다.
        log.error("H/W 지문을 수집하지 못했다 — 라이선스 검증이 불가능하다")
    return Fingerprint(components=components)


def from_components(components: dict[str, str]) -> Fingerprint:
    """저장된 부품 해시로 지문을 복원한다(라이선스 파일에서 읽을 때)."""
    return Fingerprint(components={k: v for k, v in components.items() if k in COMPONENT_ORDER})
