from datetime import datetime, timedelta, timezone

import aiosqlite
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db import get_db
from app.settings import get_config, get_settings

ROLES = ("admin", "ingestor", "user")

# Low memory cost so hashing fits a 1 GB VM.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _secret() -> str:
    secret = get_settings().jwt_secret
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters")
    return secret


def create_access_token(username: str, role: str) -> str:
    minutes = get_config().auth.access_token_minutes
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return jwt.encode({"sub": username, "role": role, "exp": exp}, _secret(), algorithm="HS256")


async def get_current_user(
    token: str = Depends(oauth2), db: aiosqlite.Connection = Depends(get_db)
) -> aiosqlite.Row:
    err = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise err
    cur = await db.execute(
        "SELECT user_id, username, role, is_active FROM users WHERE username = ?",
        (payload.get("sub"),),
    )
    user = await cur.fetchone()
    if user is None or not user["is_active"]:
        raise err
    return user


def require_roles(*allowed: str):
    async def checker(user: aiosqlite.Row = Depends(get_current_user)) -> aiosqlite.Row:
        if user["role"] not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return checker
