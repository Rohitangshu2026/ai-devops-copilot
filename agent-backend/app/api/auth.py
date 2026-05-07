"""Simple API-key guard for admin and operator endpoints.

The key is read from the ADMIN_API_KEY environment variable.  If the variable
is not set the guard is disabled so local development works without config.

Usage (FastAPI Depends):
    @router.post("/admin/reload-policy")
    async def reload(auth: None = Depends(require_admin_key)):
        ...
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


def require_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    """Raise 401 if ADMIN_API_KEY is set and the header does not match."""
    expected = os.getenv("ADMIN_API_KEY", "")
    if not expected:
        # Key not configured — guard disabled (dev / local mode)
        return
    if x_admin_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
