import hmac
import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-internal-key")

bearer = HTTPBearer()


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> CurrentUser:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return CurrentUser(
            id=int(payload["sub"]),
            email=payload.get("email", ""),
            role=payload.get("role", "customer"),
            token=creds.credentials,
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin privileges required")
    return user


def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    """Guards endpoints that only other services may call."""
    if not x_internal_token or not hmac.compare_digest(x_internal_token, INTERNAL_API_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal endpoint")
