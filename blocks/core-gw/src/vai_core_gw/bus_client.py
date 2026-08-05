"""CORE-BUS 내부 API 클라이언트.

블록 간 통신은 반드시 계약(HTTP/이벤트)을 거친다는 규칙 때문에 게이트웨이는
세션 저장소를 직접 만지지 않는다. 대신 이 얇은 클라이언트를 쓴다.
"""

from __future__ import annotations

import httpx

from vai_contracts.session import Session, SessionCreate


class CoreBusError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CoreBusClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # client 주입은 테스트에서 CORE-BUS 앱을 ASGI로 직접 물리기 위한 통로다.
        # 실제 HTTP를 태우지 않고도 블록 간 계약을 그대로 검증할 수 있다.
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def create_session(self, payload: SessionCreate) -> Session:
        response = await self._client.post(
            "/internal/v1/sessions", json=payload.model_dump(mode="json")
        )
        self._raise_for_status(response, "세션 생성 실패")
        return Session.model_validate(response.json())

    async def get_session(self, session_id: str) -> Session | None:
        response = await self._client.get(f"/internal/v1/sessions/{session_id}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_status(response, "세션 조회 실패")
        return Session.model_validate(response.json())

    async def close_session(self, session_id: str) -> Session | None:
        response = await self._client.post(f"/internal/v1/sessions/{session_id}/close")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_status(response, "세션 종료 실패")
        return Session.model_validate(response.json())

    @staticmethod
    def _raise_for_status(response: httpx.Response, message: str) -> None:
        if response.is_error:
            raise CoreBusError(f"{message}: {response.status_code}", response.status_code)

    async def aclose(self) -> None:
        await self._client.aclose()
