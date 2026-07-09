from __future__ import annotations

import importlib
import sys

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("playwright")


def _fresh_server(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK2API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROK2API_HOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROK2API_API_KEY", "test-key")
    monkeypatch.setenv("GROK2API_ADMIN_KEY", "admin-key")
    for name in list(sys.modules):
        if name == "grok2api" or name.startswith("grok2api."):
            sys.modules.pop(name)
    return importlib.import_module("grok2api.server")


def test_models_endpoint_requires_api_key(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)
    client = TestClient(server.app)

    assert client.get("/v1/models").status_code == 401
    response = client.get("/v1/models", headers={"Authorization": "Bearer test-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert {item["id"] for item in body["data"]} >= {"grok-web", "grok-imagine", "grok-video"}


def test_chat_without_ready_account_is_strict_409(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={"model": "grok-web", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "provider_login_required"

    tasks = client.get("/admin/api/tasks", headers={"Authorization": "Bearer admin-key"})
    assert tasks.status_code == 200
    assert tasks.json()["tasks"][0]["status"] == "failed"
    assert tasks.json()["tasks"][0]["kind"] == "chat"


def test_admin_page_is_public_shell_but_api_requires_admin_key(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)
    client = TestClient(server.app)

    assert client.get("/admin").status_code == 200
    assert client.get("/admin/api/accounts").status_code == 401
    assert client.get("/admin/api/accounts", headers={"Authorization": "Bearer admin-key"}).status_code == 200


def test_chat_stream_returns_sse_chunks(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)

    async def fake_chat_completion(_body, _account_id=None):
        return {"task_id": "task-chat-test", "account_id": "acct", "content": "hello stream"}

    monkeypatch.setattr(server.browser_kernel, "chat_completion", fake_chat_completion)
    client = TestClient(server.app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={
            "model": "grok-web",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "hello stream" in response.text
    assert "data: [DONE]" in response.text


def test_image_b64_is_saved_to_public_file(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)

    async def fake_image_generation(_body, _account_id=None):
        return [{"b64_json": "aGVsbG8=", "media_type": "image/png"}]

    monkeypatch.setattr(server.browser_kernel, "image_generation", fake_image_generation)
    client = TestClient(server.app)

    response = client.post(
        "/v1/images/generations",
        headers={"Authorization": "Bearer test-key"},
        json={"model": "grok-imagine", "prompt": "tiny test image"},
    )

    assert response.status_code == 200
    url = response.json()["data"][0]["url"]
    assert "/v1/files/image-" in url

    file_path = url.split("http://testserver", 1)[-1]
    file_response = client.get(file_path)
    assert file_response.status_code == 200
    assert file_response.content == b"hello"


def test_video_b64_is_saved_to_public_file(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _fresh_server(monkeypatch, tmp_path)

    async def fake_video_generation(_body, _account_id=None):
        return {
            "status": "completed",
            "videos": [{"b64_json": "dmlkZW8=", "media_type": "video/mp4"}],
        }

    monkeypatch.setattr(server.browser_kernel, "video_generation", fake_video_generation)
    client = TestClient(server.app)

    response = client.post(
        "/v1/video/generations",
        headers={"Authorization": "Bearer test-key"},
        json={"model": "grok-video", "prompt": "tiny test video"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["video_url"] == body["videos"][0]["url"]
    assert "/v1/files/video-" in body["video_url"]

    file_path = body["video_url"].split("http://testserver", 1)[-1]
    file_response = client.get(file_path)
    assert file_response.status_code == 200
    assert file_response.content == b"video"
