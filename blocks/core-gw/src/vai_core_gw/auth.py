"""인증 — Wave 1 범위의 JWT 발급/검증.

지금은 대칭키(HS256) 자체 발급이다. Wave 6에서 고객사 SSO(OIDC/SAML) 연동을
붙일 때 이 모듈의 :func:`verify_access_token`만 교체하면 되도록, 나머지 코드는
:class:`Principal`만 알고 토큰 형식을 모른다.

세션 토큰을 따로 두는 이유: 웹 위젯은 브라우저에 API 자격증명을 둘 수 없다.
서버가 세션을 만들 때 **그 세션에만** 유효한 단기 토큰을 발급하고, WebSocket은
그 토큰만 받는다. 토큰이 유출돼도 해당 상담 하나로 피해가 갇힌다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"
ISSUER = "visionai"
SESSION_TOKEN_TTL = timedelta(hours=4)
"""상담 한 건의 최대 길이를 넉넉히 덮는 값."""


class AuthError(Exception):
    """인증 실패."""


@dataclass(frozen=True)
class Principal:
    """인증된 호출자."""

    tenant_id: str
    subject: str
    scopes: frozenset[str] = frozenset()

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthError(f"'{scope}' 권한이 없다")


@dataclass(frozen=True)
class SessionToken:
    session_id: str
    tenant_id: str


def _decode(token: str, secret: str, expected_type: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[ALGORITHM], issuer=ISSUER, options={"require": ["exp"]}
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"토큰 검증 실패: {exc}") from exc
    if claims.get("typ") != expected_type:
        raise AuthError("토큰 용도가 다르다")
    return claims


def verify_access_token(token: str, secret: str) -> Principal:
    """관리/서버 간 호출용 액세스 토큰."""
    claims = _decode(token, secret, "access")
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise AuthError("tenant_id 클레임이 없다")
    return Principal(
        tenant_id=str(tenant_id),
        subject=str(claims.get("sub", "unknown")),
        scopes=frozenset(claims.get("scopes", [])),
    )


def issue_session_token(session_id: str, tenant_id: str, secret: str) -> str:
    """WebSocket 접속 전용 단기 토큰."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "typ": "session",
            "iss": ISSUER,
            "sub": session_id,
            "tenant_id": tenant_id,
            "iat": now,
            "exp": now + SESSION_TOKEN_TTL,
        },
        secret,
        algorithm=ALGORITHM,
    )


def verify_session_token(token: str, secret: str) -> SessionToken:
    claims = _decode(token, secret, "session")
    return SessionToken(session_id=str(claims["sub"]), tenant_id=str(claims["tenant_id"]))


def issue_access_token(
    tenant_id: str, subject: str, secret: str, *, scopes: list[str] | None = None
) -> str:
    """개발·테스트용 액세스 토큰 발급기 (운영에서는 IdP가 대신한다)."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "typ": "access",
            "iss": ISSUER,
            "sub": subject,
            "tenant_id": tenant_id,
            "scopes": scopes or ["session:create"],
            "iat": now,
            "exp": now + timedelta(hours=12),
        },
        secret,
        algorithm=ALGORITHM,
    )
