"""Minimal identity for this round: a caller-supplied username header.

This is the placeholder for the api-gateway authentication responsibility
(spec 3.1). Real login replaces this one function; nothing downstream reads
the request object.
"""

from fastapi import Header, HTTPException, status


def get_current_username(x_username: str | None = Header(default=None)) -> str:
    if not x_username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少身份信息"
        )
    return x_username