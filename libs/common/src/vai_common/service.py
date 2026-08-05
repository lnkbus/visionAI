"""블록 서비스 부트스트랩.

모든 블록이 같은 방식으로 기동하도록 공통 절차를 한곳에 모은다:
로깅 구성 → 라이선스 게이트 확인 → 헬스 엔드포인트 노출.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI

from vai_common.license import LicenseGate
from vai_common.logging import configure_logging
from vai_common.settings import CommonSettings, get_settings

log = logging.getLogger(__name__)

Lifespan = Callable[[FastAPI], Any]


def create_block_app(
    *,
    block_id: str,
    title: str,
    settings: CommonSettings | None = None,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    """블록용 FastAPI 앱을 만든다.

    ``lifespan``으로 블록 고유의 기동/종료 처리를 넘기면 공통 절차 뒤에 실행된다.
    """
    cfg = settings or get_settings()
    configure_logging(block_id, cfg.log_level, as_json=cfg.log_json)

    gate = LicenseGate.load(cfg.license_path)
    gate.require(block_id)  # 미허가면 여기서 기동이 중단된다
    if gate.dev_mode:
        log.info("개발 모드 기동", extra={"block": block_id})

    @asynccontextmanager
    async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
        log.info("블록 기동", extra={"block": block_id, "env": cfg.environment})
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan(application))
            yield
        log.info("블록 종료", extra={"block": block_id})

    app = FastAPI(title=title, version="0.1.0", lifespan=_lifespan)
    app.state.block_id = block_id
    app.state.settings = cfg
    app.state.license = gate

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """프로세스 생존 확인 (K8s liveness)."""
        return {"status": "ok", "block": block_id}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, object]:
        """트래픽 수용 준비 확인 (K8s readiness)."""
        return {
            "status": "degraded" if gate.is_expired() else "ready",
            "block": block_id,
            "license_expired": gate.is_expired(),
        }

    return app


def serve(app: FastAPI, *, default_port: int) -> None:
    """블록 서버를 기동한다.

    포트를 하드코딩하면 한 호스트에서 여러 블록을 띄우는 구성(compose 데모,
    스택 스모크 테스트, 단일 노드 PoC)에서 충돌한다. ``VAI_PORT``로 덮어쓴다.
    """
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("VAI_HOST", "0.0.0.0"),
        port=int(os.environ.get("VAI_PORT", default_port)),
        log_config=None,  # configure_logging이 세운 핸들러를 uvicorn이 덮어쓰지 않게 한다
    )
