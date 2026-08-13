from __future__ import annotations

import hmac

from fastapi import Header, HTTPException


def verify_bearer(expected: str | None, authorization: str | None = Header(default=None)) -> None:
    if expected is None:
        return
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len(prefix) :]
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid bearer token")
