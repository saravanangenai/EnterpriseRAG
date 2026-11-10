import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from app.auth import (
    ROLES,
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    verify_password,
)
from app.db import get_db

router = APIRouter(tags=["auth"])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10)
    role: str


@router.post("/auth/login")
async def login(
    form: OAuth2PasswordRequestForm = Depends(), db: aiosqlite.Connection = Depends(get_db)
):
    cur = await db.execute(
        "SELECT username, password_hash, role, is_active FROM users WHERE username = ?",
        (form.username,),
    )
    user = await cur.fetchone()
    if user is None or not user["is_active"] or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    return {
        "access_token": create_access_token(user["username"], user["role"]),
        "token_type": "bearer",
    }


@router.get("/auth/me")
async def me(user: aiosqlite.Row = Depends(get_current_user)):
    return {"username": user["username"], "role": user["role"]}


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: aiosqlite.Row = Depends(require_roles("admin")),
):
    if body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (body.username, hash_password(body.password), body.role),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "Username already exists")
    return {"username": body.username, "role": body.role}
