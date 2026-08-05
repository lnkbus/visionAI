"""라이선스 발급요청 생성과 설치.

버스·HTTP와 무관한 순수 로직으로 분리했다. 라이선스는 잘못 설치되면 고객사
전체가 기동하지 못하므로, 경계 조건(서명 위조·만료·지문 불일치·부분 쓰기)을
단위 테스트로 고정한다.

**설치 검증과 기동 검증은 같은 코드다.** :class:`~vai_common.license.LicenseGate`
를 그대로 쓴다. 설치기가 자체 검증 로직을 따로 두면 "설치는 됐는데 재기동에서
죽는" 최악의 실패가 생긴다.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vai_common import fingerprint as fp
from vai_common.license import LicenseError, LicenseGate
from vai_contracts.licensing import FingerprintReport, LicenseRequest, LicenseView

log = logging.getLogger(__name__)


def current_fingerprint(*, include_gpu: bool = True) -> FingerprintReport:
    """이 서버의 지문을 수집해 보고서 형태로 만든다."""
    snapshot = fp.collect(include_gpu=include_gpu)
    return FingerprintReport(
        combined=snapshot.combined,
        components=dict(snapshot.components),
        hostname=socket.gethostname(),
        collected_at=datetime.now(UTC),
    )


def build_request(
    customer_id: str,
    *,
    site: str = "",
    contact: str = "",
    notes: str = "",
    requested_blocks: list[str] | None = None,
    include_gpu: bool = True,
) -> LicenseRequest:
    """발급요청서를 만든다. 이 파일만 반출하면 발급이 가능하다."""
    report = current_fingerprint(include_gpu=include_gpu)
    if not report.components:
        # 지문이 비면 어느 서버에서든 도는 라이선스가 발급된다. 그건 DRM이 아니다.
        raise LicenseError(
            "H/W 지문을 하나도 수집하지 못했다 — 이 서버로는 라이선스를 묶을 수 없다"
        )
    return LicenseRequest(
        customer_id=customer_id,
        site=site,
        fingerprint=report,
        requested_blocks=sorted(requested_blocks or []),
        contact=contact,
        notes=notes,
    )


def _to_view(gate: LicenseGate, *, installed: bool) -> LicenseView:
    status = gate.status()
    return LicenseView(
        installed=installed,
        customer_id=status.customer_id,
        expires=status.expires,
        days_remaining=status.days_remaining,
        in_grace=status.in_grace,
        dev_mode=status.dev_mode,
        signature_verified=status.signature_verified,
        fingerprint_ok=status.fingerprint_ok,
        blocks=status.blocks,
        capacity=status.capacity,
    )


class LicenseInstaller:
    """``.lic`` 설치 창구.

    설치 경로(``VAI_LICENSE_PATH``)는 모든 블록이 읽는 그 파일이다. 컨테이너
    구성에서는 공유 볼륨으로 마운트한다 — CORE-LIC이 쓰고 나머지는 읽기 전용이다.
    """

    def __init__(
        self,
        license_path: str | None,
        *,
        public_key_pem: bytes | None = None,
        verify_fingerprint: bool = True,
    ) -> None:
        self._path = Path(license_path) if license_path else None
        self._public_key = public_key_pem
        self._verify_fingerprint = verify_fingerprint

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def signing_enforced(self) -> bool:
        """공개키가 있어 서명 위조를 실제로 막고 있는지."""
        return self._public_key is not None

    def current(self) -> LicenseView:
        """설치된 라이선스 현황. 파일이 없거나 깨졌어도 예외를 던지지 않는다.

        운영 콘솔이 "왜 안 되는지" 보러 오는 화면이다. 여기서 500이 나면
        운영자는 아무 단서도 못 얻는다.
        """
        if self._path is None or not self._path.is_file():
            return LicenseView(installed=False)
        try:
            gate = self._verify(json.loads(self._path.read_text(encoding="utf-8")))
        except (LicenseError, ValueError) as exc:
            log.warning("설치된 라이선스를 읽을 수 없다", extra={"reason": str(exc)})
            return LicenseView(installed=True, signature_verified=False)
        return _to_view(gate, installed=True)

    def verify(self, content: dict[str, Any]) -> LicenseView:
        """설치하지 않고 검증만 한다 — 반입 직후 사전 점검용."""
        return _to_view(self._verify(content), installed=False)

    def install(self, content: dict[str, Any]) -> LicenseView:
        """검증 후 설치한다. 검증에 실패하면 기존 파일은 손대지 않는다."""
        if self._path is None:
            raise LicenseError("라이선스 경로(VAI_LICENSE_PATH)가 설정되지 않아 설치할 수 없다")
        gate = self._verify(content)
        self._write_atomic(content)
        status = gate.status()
        log.info(
            "라이선스 설치 완료",
            extra={
                "customer_id": status.customer_id,
                "expires": str(status.expires),
                "blocks": len(status.blocks),
            },
        )
        return _to_view(gate, installed=True)

    def _verify(self, content: dict[str, Any]) -> LicenseGate:
        return LicenseGate.from_document(
            content,
            public_key_pem=self._public_key,
            verify_fingerprint=self._verify_fingerprint,
        )

    def _write_atomic(self, content: dict[str, Any]) -> None:
        """같은 디렉토리에 임시 파일로 쓴 뒤 rename 한다.

        직접 덮어쓰다가 프로세스가 죽으면 잘린 JSON이 남고, 그때부터 **모든
        블록이 기동하지 못한다.** rename은 같은 파일시스템 안에서 원자적이다.
        """
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(content, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
