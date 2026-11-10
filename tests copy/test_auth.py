import pytest
from fastapi.testclient import TestClient

from app import settings
from app.auth import hash_password


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("CONFIG_PATH", "config/config.yaml")
    settings.get_settings.cache_clear()
    settings.get_config.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        import sqlite3

        con = sqlite3.connect(tmp_path / "app.db")
        con.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("admin", hash_password("correct-horse-battery"), "admin"),
        )
        con.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("bob", hash_password("another-long-password"), "user"),
        )
        con.commit()
        con.close()
        yield c


def login(client, user, pw):
    return client.post("/auth/login", data={"username": user, "password": pw})


def test_health_reports_providers(client):
    body = client.get("/health").json()
    assert body["llm"].startswith("groq:")
    assert body["embedding"].startswith("openai:")
    assert body["collection"].endswith("_768")


def test_login_and_me(client):
    token = login(client, "admin", "correct-horse-battery").json()["access_token"]
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json() == {"username": "admin", "role": "admin"}


def test_bad_password_rejected(client):
    assert login(client, "admin", "wrong-password-here").status_code == 401


def test_no_token_rejected(client):
    assert client.get("/auth/me").status_code == 401


def test_only_admin_creates_users(client):
    body = {"username": "carol", "password": "a-long-password", "role": "ingestor"}
    bob = login(client, "bob", "another-long-password").json()["access_token"]
    assert client.post("/users", json=body, headers={"Authorization": f"Bearer {bob}"}).status_code == 403
    adm = login(client, "admin", "correct-horse-battery").json()["access_token"]
    assert client.post("/users", json=body, headers={"Authorization": f"Bearer {adm}"}).status_code == 201
    assert client.post("/users", json=body, headers={"Authorization": f"Bearer {adm}"}).status_code == 409
