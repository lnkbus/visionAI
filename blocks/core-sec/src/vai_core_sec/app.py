"""CORE-SEC — 암호화·감사 블록.

두 가지를 맡는다.

1. **감사 저장소** — 해시 체인 append-only. 변조를 막지는 못하지만
   *탐지*하고, 어디서부터 믿을 수 없는지 짚어 준다.
2. **키 관리 창구** — 마스터 키 상태 조회와 교체(rewrap) 지원.
   암복호 자체는 각 블록이 인프로세스로 한다(``vai_common.crypto``) —
   저장 경로마다 네트워크 왕복을 넣으면 이 블록이 전 블록의 병목이자
   단일 장애점이 된다.

기록 경로가 둘인 이유:

* ``audit.log`` 토픽(비동기) — 일반 기록. 저장소가 밀려도 상담은 계속 돈다
* ``POST /internal/v1/audit``(동기) — **기록 성공 후 허용**이 필요한 행위.
  PII 원문 열람이 대표적이다. 열람은 됐는데 기록은 실패한 상태가 남으면
  감사에서 그 열람은 없었던 일이 된다
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import EventBus, build_bus
from vai_common.crypto import CryptoError, Vault, create_cipher, load_master_key
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.audit import AuditAction, AuditEvent, AuditRecord, ChainStatus
from vai_core_sec.chain import AuditChain
from vai_core_sec.worker import BLOCK_ID, AuditWorker

log = logging.getLogger(__name__)


class SecuritySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_SEC_", extra="ignore")

    audit_dir: str = "/var/lib/visionai/audit"
    """감사 세그먼트 디렉토리. 0700으로 강제된다."""

    master_key_path: str = ""
    """저장 암호화 마스터 키(0600). 비면 암호화 기능이 꺼진 채로 뜬다 —
    감사 기능만 쓰는 구성도 있으므로 기동을 막지는 않는다."""

    cipher: str = "aes-gcm"
    """``kcmvp``로 바꾸면 검증모듈 연동 어댑터를 쓴다."""

    key_id: str = "k1"


class KeyStatus(BaseModel):
    """키 상태. 운영 콘솔이 "암호화가 실제로 켜져 있는지" 확인하는 화면의 데이터원.

    제안서에는 암호화가 있는데 현장에서는 꺼져 있는 상태가 이 영역의 대표 사고다.
    """

    enabled: bool
    algorithm: str = ""
    key_id: str = ""
    reason: str = ""


def _build_vault(cfg: SecuritySettings) -> tuple[Vault | None, str]:
    if not cfg.master_key_path:
        return None, "마스터 키 경로가 지정되지 않았다 (VAI_SEC_MASTER_KEY_PATH)"
    try:
        key = load_master_key(cfg.master_key_path)
    except CryptoError as exc:
        # 기동을 막지는 않되 감추지도 않는다. 조용히 평문 저장으로 도는 것이
        # 최악이므로 상태 API가 이유를 그대로 노출한다.
        log.error("마스터 키를 읽을 수 없다", extra={"reason": str(exc)})
        return None, str(exc)
    return Vault(key, cipher=create_cipher(cfg.cipher), key_id=cfg.key_id), ""


def create_app(bus: EventBus | None = None, chain: AuditChain | None = None) -> FastAPI:
    common = get_settings()
    cfg = SecuritySettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.bus = bus or build_bus(common.redis_url)
        worker = AuditWorker(
            application.state.bus,
            application.state.chain,
            group=common.consumer_group,
            consumer=common.consumer_name,
        )
        task = asyncio.create_task(worker.run(), name="audit-worker")
        try:
            yield
        finally:
            worker.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if not injected:
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI CORE-SEC", settings=common, lifespan=lifespan
    )

    store = chain or AuditChain(cfg.audit_dir)
    store.load()
    app.state.chain = store

    vault, vault_reason = _build_vault(cfg)
    app.state.vault = vault
    app.state.vault_reason = vault_reason
    if vault is None:
        log.warning("저장 암호화 비활성 — 평문 저장", extra={"reason": vault_reason})

    @app.post(
        "/internal/v1/audit",
        response_model=AuditRecord,
        status_code=status.HTTP_201_CREATED,
        tags=["audit"],
    )
    async def record(event: AuditEvent, request: Request) -> AuditRecord:
        """동기 기록 — 반환된 뒤에는 디스크에 있다.

        PII 원문 열람처럼 "기록 성공 후 허용"이 필요한 경로가 이 응답을
        신뢰한다. 호출부는 201을 받은 뒤에야 열람을 허용해야 한다.
        """
        chain_: AuditChain = request.app.state.chain
        try:
            return await chain_.append(event)
        except OSError as exc:
            # 기록에 실패했으면 호출부는 열람을 허용하면 안 된다.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"감사 기록 실패: {exc}"
            ) from exc

    @app.get("/internal/v1/audit", response_model=list[AuditRecord], tags=["audit"])
    async def query(
        request: Request,
        actor: str = "",
        action: AuditAction | None = None,
        tenant_id: str = "",
        session_id: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> list[AuditRecord]:
        """감사 조회 — 최신 것부터."""
        chain_: AuditChain = request.app.state.chain
        return chain_.query(
            actor=actor,
            action=action,
            tenant_id=tenant_id,
            session_id=session_id,
            since=since,
            until=until,
            limit=limit,
        )

    @app.get("/internal/v1/audit/verify", response_model=ChainStatus, tags=["audit"])
    async def verify(request: Request) -> ChainStatus:
        """무결성 검증. 감사 대응의 첫 화면이다.

        전체를 재계산하므로 레코드가 많으면 수 초가 걸린다. 스레드로 넘겨
        검증 중에도 기록이 계속 받아지게 한다.
        """
        chain_: AuditChain = request.app.state.chain
        return await asyncio.to_thread(chain_.verify)

    @app.get("/internal/v1/crypto/status", response_model=KeyStatus, tags=["crypto"])
    async def key_status(request: Request) -> KeyStatus:
        """암호화가 실제로 켜져 있는지. 운영 콘솔이 주기적으로 확인한다."""
        vault_: Vault | None = request.app.state.vault
        if vault_ is None:
            return KeyStatus(enabled=False, reason=request.app.state.vault_reason)
        return KeyStatus(enabled=True, algorithm=vault_.algorithm, key_id=vault_.key_id)

    return app
