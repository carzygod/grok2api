from __future__ import annotations

import time
import uuid
import json
import base64
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .browser_adapter import BrowserAdapterError
from .browser_kernel import BrowserKernel
from .config import settings
from .media_store import MediaStore
from .models import (
    AccountCreate,
    AccountUpdate,
    ChatCompletionRequest,
    ChatMessage,
    GenerationQuotaUpdate,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageVariationRequest,
    ResponsesRequest,
    VideoGenerationRequest,
)
from .store import AccountStore


store = AccountStore()
browser_kernel = BrowserKernel(store)
media_store = MediaStore()


MODEL_SPECS = [
    {
        "id": "grok-web",
        "name": "Grok Web Text",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["chat", "vision"],
    },
    {
        "id": "grok-vision",
        "name": "Grok Web Vision",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["chat", "vision"],
    },
    {
        "id": "grok-imagine",
        "name": "Grok Imagine Image",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["image"],
    },
    {
        "id": "grok-imagine-edit",
        "name": "Grok Imagine Image Edit",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["image", "image_edit"],
    },
    {
        "id": "grok-imagine-variation",
        "name": "Grok Imagine Image Variation",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["image", "image_variation"],
    },
    {
        "id": "grok-video",
        "name": "Grok Imagine Video",
        "object": "model",
        "created": 1783580000,
        "owned_by": "xai-web",
        "capabilities": ["video"],
    },
]


def _require_admin(key: str | None = None, authorization: str | None = None) -> None:
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    expected = settings.effective_admin_key
    if key != expected and bearer != expected:
        raise HTTPException(status_code=401, detail="invalid_admin_key")


def _require_api_key(authorization: str | None = None) -> None:
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    if bearer != settings.effective_api_key:
        raise HTTPException(status_code=401, detail="invalid_api_key")


async def admin_auth(
    key: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _require_admin(key, authorization)


async def api_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    _require_api_key(authorization)


def create_app() -> FastAPI:
    app = FastAPI(title="grok2api", version="0.1.0")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "service": "grok2api",
            "time": int(time.time()),
            "browser_mode": settings.browser_mode,
        }

    def _capabilities() -> dict:
        return {
            "service": "grok2api",
            "browser": {
                "mode": settings.browser_mode,
                "image": settings.browser_image,
                "base_url": settings.browser_base_url,
                "docker_socket_required": settings.browser_mode == "docker-novnc",
            },
            "auth": {
                "api_key": settings.effective_api_key,
                "authorization_header": f"Bearer {settings.effective_api_key}",
            },
            "adapters": {
                "text": {
                    "status": "browser_cdp_adapter",
                    "target": "https://grok.com/",
                    "reason": "CDP adapter drives the logged-in account browser, supports text and image/video attachments, and requires a ready account profile.",
                },
                "image": {
                    "status": "browser_cdp_adapter",
                    "target": "https://grok.com/imagine",
                    "reason": "CDP adapter drives Grok Imagine image mode, uploads optional reference media, and extracts generated image nodes from the page.",
                },
                "video": {
                    "status": "browser_cdp_adapter_with_task_log",
                    "target": "https://grok.com/imagine",
                    "reason": "CDP adapter submits the prompt, applies request constraints, waits for media nodes, and stores task state.",
                },
            },
        }

    def _adapter_http_error(exc: BrowserAdapterError) -> HTTPException:
        headers = None
        retry_after = exc.details.get("retry_after")
        if retry_after is not None:
            headers = {"Retry-After": str(max(0, int(retry_after)))}
        return HTTPException(status_code=exc.status_code, detail=exc.payload(), headers=headers)

    def _chat_content(result: str | dict) -> tuple[str, str | None, str | None]:
        if isinstance(result, dict):
            return (
                str(result.get("content", "")),
                result.get("task_id"),
                result.get("account_id"),
            )
        return result, None, None

    def _image_data(result: list | dict) -> tuple[list[dict], str | None, str | None]:
        if isinstance(result, dict):
            return (
                list(result.get("data") or []),
                result.get("task_id"),
                result.get("account_id"),
            )
        return list(result or []), None, None

    async def _upload_to_data_url(file: UploadFile) -> str:
        raw = await file.read()
        media_type = file.content_type or "application/octet-stream"
        return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"

    def _coerce_int_field(payload: dict, key: str) -> None:
        if key in payload and payload[key] not in {None, ""}:
            try:
                payload[key] = int(payload[key])
            except (TypeError, ValueError):
                pass

    async def _payload_from_request(request: Request) -> dict:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type.lower():
            try:
                data = await request.json()
            except Exception:
                data = {}
            return dict(data or {})

        form = await request.form()
        payload: dict = {}
        media: list[str] = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                data_url = await _upload_to_data_url(value)
                if key in {"image", "images", "file", "files", "mask"}:
                    media.append(data_url)
                else:
                    payload[key] = data_url
                continue
            if key in {"image", "images", "file", "files"}:
                text = str(value)
                if text:
                    media.append(text)
            else:
                payload[key] = value
        if media:
            payload["image"] = media if len(media) > 1 else media[0]
        for key in ("n", "duration"):
            _coerce_int_field(payload, key)
        return payload

    async def _image_request_from_request(
        request: Request,
        cls: type[ImageGenerationRequest],
        *,
        default_prompt: str | None = None,
    ) -> ImageGenerationRequest:
        payload = await _payload_from_request(request)
        if default_prompt and not str(payload.get("prompt") or "").strip():
            payload["prompt"] = default_prompt
        return cls.model_validate(payload)

    async def _video_request_from_request(request: Request) -> VideoGenerationRequest:
        return VideoGenerationRequest.model_validate(await _payload_from_request(request))

    def _responses_messages(body: ResponsesRequest) -> list[ChatMessage]:
        if isinstance(body.input, str):
            return [ChatMessage(role="user", content=body.input)]
        allowed_roles = {"system", "developer", "user", "assistant", "tool"}
        messages: list[ChatMessage] = []
        for item in body.input:
            if isinstance(item, str):
                messages.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role not in allowed_roles:
                role = "user"
            content = item.get("content")
            if content is None and item.get("type") in {"input_text", "input_image", "image_url"}:
                content = [item]
            if content is None:
                content = item.get("text") or item.get("input") or ""
            if isinstance(content, list):
                messages.append(ChatMessage(role=role, content=content))
            else:
                messages.append(ChatMessage(role=role, content=str(content)))
        return messages

    def _response_object(
        *,
        response_id: str,
        created: int,
        model: str,
        content: str,
        task_id: str | None,
        account_id: str | None,
    ) -> dict:
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "model": model,
            "status": "completed",
            "task_id": task_id,
            "account_id": account_id,
            "output": [
                {
                    "id": "msg-" + uuid.uuid4().hex,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ],
            "output_text": content,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

    def _json_sse(data: dict) -> str:
        return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    async def _stream_chat_response(
        *,
        chat_id: str,
        created: int,
        model: str,
        content: str,
    ):
        yield _json_sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        )
        for index in range(0, len(content), 1024):
            yield _json_sse(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content[index : index + 1024]},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        yield _json_sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    async def _stream_responses_response(response: dict):
        yield "event: response.created\n"
        yield _json_sse({key: value for key, value in response.items() if key != "output_text"})
        text = response.get("output_text", "")
        for index in range(0, len(text), 1024):
            yield "event: response.output_text.delta\n"
            yield _json_sse({"delta": text[index : index + 1024]})
        yield "event: response.completed\n"
        yield _json_sse(response)
        yield "data: [DONE]\n\n"

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>grok2api Admin</title>
  <style>
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#0b1018; color:#e8eef7; }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:20px; }
    h1 { font-size: 28px; margin: 0 0 6px; letter-spacing:0; }
    p { color:#98a8bc; line-height:1.6; margin:0; }
    button, input, textarea { font: inherit; }
    input, textarea { width:100%; border:1px solid #26364b; border-radius:12px; background:#101824; color:#e8eef7; padding:11px 12px; outline:none; }
    textarea { min-height: 90px; resize: vertical; }
    button { border:0; border-radius:12px; padding:10px 14px; background:#30d7a6; color:#06110d; cursor:pointer; font-weight:700; }
    button.secondary { background:#1e2a3a; color:#d9e7f7; border:1px solid #31445c; }
    button.danger { background:#e86565; color:#190606; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:14px; margin-bottom:16px; }
    .panel { border:1px solid #223149; border-radius:18px; padding:18px; background:#121a27; box-shadow: 0 16px 42px rgba(0,0,0,.24); }
    .panel h2 { margin:0 0 12px; font-size:17px; }
    .muted { color:#98a8bc; }
    .row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .stack { display:grid; gap:10px; }
    .accounts { display:grid; gap:12px; }
    .account { border:1px solid #26364b; border-radius:16px; padding:14px; background:#0f1723; }
    .account-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; }
    .badge { display:inline-flex; align-items:center; border-radius:999px; padding:4px 9px; background:#1d2a3b; color:#bfd0e4; font-size:12px; }
    .badge.ready { background:#12392e; color:#8df0c9; }
    .badge.warn { background:#3a2e12; color:#f5d889; }
    .quota-table { width:100%; border-collapse:collapse; overflow:hidden; }
    .quota-table th, .quota-table td { border-bottom:1px solid #26364b; padding:9px 8px; text-align:left; font-size:13px; }
    .quota-table th { color:#98a8bc; font-weight:600; }
    pre { white-space:pre-wrap; overflow:auto; border:1px solid #26364b; border-radius:14px; background:#08101a; color:#cfe0f4; padding:14px; min-height:80px; }
    a { color:#7bd7ff; }
    .key-grid { display:grid; grid-template-columns: 1fr auto; gap:10px; align-items:center; }
    .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    @media (max-width: 900px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } header { align-items:flex-start; flex-direction:column; } }
    @media (max-width: 560px) { .grid { grid-template-columns: 1fr; } main { padding:18px; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>grok2api Admin</h1>
        <p>Account pool, remote Chromium login, and Grok Web adapter readiness.</p>
      </div>
      <button id="refreshBtn" class="secondary">Refresh</button>
    </header>

    <section class="grid">
      <div class="panel"><h2>Remote Browser</h2><p id="browserStatus" class="muted">Loading</p></div>
      <div class="panel"><h2>Text</h2><p id="textStatus" class="muted">Loading</p></div>
      <div class="panel"><h2>Image</h2><p id="imageStatus" class="muted">Loading</p></div>
      <div class="panel"><h2>Video</h2><p id="videoStatus" class="muted">Loading</p></div>
    </section>

    <section class="panel stack" style="margin-bottom:16px;">
      <h2>API Access</h2>
      <p class="muted">Use this key for OpenAI-compatible endpoints such as <span class="mono">/v1/responses</span>, <span class="mono">/v1/images/generations</span>, and <span class="mono">/v1/videos</span>.</p>
      <div class="key-grid">
        <input id="apiKey" class="mono" readonly placeholder="Loading API key" />
        <button id="copyApiKey" class="secondary">Copy</button>
      </div>
      <pre id="apiExample" class="mono"></pre>
    </section>

    <section class="panel stack" style="margin-bottom:16px;">
      <h2>Add Account</h2>
      <input id="accountName" placeholder="Account name, for example grok-pro-01" />
      <textarea id="cookieHeader" placeholder="Optional: paste full Grok request Cookie header here"></textarea>
      <div class="row">
        <button id="addBtn">Add Account</button>
        <button id="capBtn" class="secondary">Show Capabilities</button>
      </div>
    </section>

    <section class="panel stack">
      <h2>Accounts</h2>
      <div id="accounts" class="accounts"></div>
    </section>

    <section class="panel stack" style="margin-top:16px;">
      <h2>Generation Quotas</h2>
      <div id="quotas" class="muted">Loading</div>
    </section>

    <section class="panel stack" style="margin-top:16px;">
      <h2>Output</h2>
      <pre id="output"></pre>
    </section>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
    let key = params.get("key") || hashParams.get("key") || sessionStorage.getItem("grok2api_admin_key") || "";
    if (key) {
      sessionStorage.setItem("grok2api_admin_key", key);
      history.replaceState(null, "", location.pathname);
    } else {
      key = window.prompt("Admin key") || "";
      if (key) sessionStorage.setItem("grok2api_admin_key", key);
    }
    const out = document.getElementById("output");
    const api = async (path, options = {}) => {
      const headers = {"Content-Type":"application/json"};
      if (key) headers.Authorization = "Bearer " + key;
      const res = await fetch(path, {
        headers,
        ...options
      });
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = text; }
      if (!res.ok) throw {status: res.status, data};
      return data;
    };
    const show = (value) => { out.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
    const badge = (status) => {
      const cls = status === "ready" ? "ready" : (status === "error" || status === "blocked" ? "warn" : "");
      return `<span class="badge ${cls}">${status || "unknown"}</span>`;
    };
    async function load() {
      try {
        const caps = await api("/admin/api/capabilities");
        document.getElementById("browserStatus").textContent = caps.browser.mode + " / " + caps.browser.image;
        document.getElementById("textStatus").textContent = caps.adapters.text.status;
        document.getElementById("imageStatus").textContent = caps.adapters.image.status;
        document.getElementById("videoStatus").textContent = caps.adapters.video.status;
        const apiKey = caps.auth && caps.auth.api_key ? caps.auth.api_key : "";
        document.getElementById("apiKey").value = apiKey;
        document.getElementById("apiExample").textContent = apiKey
          ? `curl -H "Authorization: Bearer ${apiKey}" ${location.origin}/v1/models`
          : "API key is not configured.";
        const data = await api("/admin/api/accounts");
        const root = document.getElementById("accounts");
        root.innerHTML = "";
        if (!data.accounts.length) {
          root.innerHTML = '<p class="muted">No account yet.</p>';
        } else {
          for (const account of data.accounts) {
            const div = document.createElement("div");
            div.className = "account";
            div.innerHTML = `
              <div class="account-head">
                <div><strong>${account.name}</strong><div class="muted">${account.id}</div></div>
                ${badge(account.status)}
              </div>
              <div class="muted">cookies: ${account.cookie_count} / port: ${account.browser_port || "-"} / debug: ${account.browser_debug_port || "-"}</div>
              <div class="muted">profile: ${account.user_data_dir}</div>
              <div class="row" style="margin-top:12px;">
                <button data-action="login" data-id="${account.id}">Open Login Browser</button>
                <button class="secondary" data-action="status" data-id="${account.id}">Browser Status</button>
                <button class="secondary" data-action="validate" data-id="${account.id}">Validate</button>
                <button class="secondary" data-action="recreate" data-id="${account.id}">Recreate Browser</button>
                <button class="danger" data-action="stop" data-id="${account.id}">Close + Delete Profile</button>
              </div>
              ${account.last_error ? `<p class="muted" style="margin-top:10px;">${account.last_error}</p>` : ""}
            `;
            root.appendChild(div);
          }
        }
        const quotaData = await api("/admin/api/quotas");
        const quotaRoot = document.getElementById("quotas");
        if (!quotaData.quotas.length) {
          quotaRoot.textContent = "No quota rows yet.";
        } else {
          quotaRoot.innerHTML = `
            <table class="quota-table">
              <thead><tr><th>Account</th><th>Kind</th><th>Used</th><th>Reserved</th><th>Remaining</th><th>Limit</th><th>Cooldown</th></tr></thead>
              <tbody>
                ${quotaData.quotas.map(q => `
                  <tr>
                    <td><span class="mono">${q.account_id}</span></td>
                    <td>${q.kind}</td>
                    <td>${q.used_units}</td>
                    <td>${q.reserved_units}</td>
                    <td>${q.remaining_units}</td>
                    <td>${q.limit_units}</td>
                    <td>${q.cooldown_active ? `${q.cooldown_reason || "cooldown"} until ${new Date(q.cooldown_until * 1000).toLocaleString()}` : "-"}</td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          `;
        }
      } catch (err) {
        show(err);
      }
    }
    document.addEventListener("click", async (event) => {
      const target = event.target;
      if (!target.dataset || !target.dataset.action) return;
      const id = target.dataset.id;
      try {
        target.disabled = true;
        let result;
        if (target.dataset.action === "login") {
          result = await api(`/admin/api/accounts/${id}/login-session`, {method:"POST"});
          show(result);
          if (result.browser_url) window.open(result.browser_url, "_blank");
        } else if (target.dataset.action === "status") {
          result = await api(`/admin/api/accounts/${id}/browser/status`);
          show(result);
        } else if (target.dataset.action === "validate") {
          result = await api(`/admin/api/accounts/${id}/validate`, {method:"POST"});
          show(result);
        } else if (target.dataset.action === "recreate") {
          result = await api(`/admin/api/accounts/${id}/browser/recreate`, {method:"POST"});
          show(result);
        } else if (target.dataset.action === "stop") {
          if (!window.confirm(`Close browser container and delete profile for ${id}? This removes the saved Grok login state.`)) return;
          result = await api(`/admin/api/accounts/${id}/browser/stop`, {method:"POST"});
          show(result);
        }
        await load();
      } catch (err) {
        show(err);
      } finally {
        target.disabled = false;
      }
    });
    document.getElementById("addBtn").onclick = async () => {
      try {
        const body = {
          name: document.getElementById("accountName").value,
          cookie_header: document.getElementById("cookieHeader").value
        };
        show(await api("/admin/api/accounts", {method:"POST", body: JSON.stringify(body)}));
        await load();
      } catch (err) { show(err); }
    };
    document.getElementById("capBtn").onclick = async () => show(await api("/admin/api/capabilities"));
    document.getElementById("copyApiKey").onclick = async () => {
      const value = document.getElementById("apiKey").value;
      if (!value) return;
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const input = document.getElementById("apiKey");
        input.focus();
        input.select();
        document.execCommand("copy");
      }
      show("API key copied.");
    };
    document.getElementById("refreshBtn").onclick = load;
    load();
  </script>
</body>
</html>
"""

    @app.get("/admin/api/capabilities", dependencies=[Depends(admin_auth)])
    async def capabilities():
        data = _capabilities()
        data["browser"]["image_status"] = browser_kernel.browser_image_status()
        data["models"] = MODEL_SPECS
        return data

    @app.get("/admin/api/browser/image-status", dependencies=[Depends(admin_auth)])
    async def browser_image_status():
        return browser_kernel.browser_image_status()

    @app.get("/admin/api/accounts", dependencies=[Depends(admin_auth)])
    async def list_accounts():
        return {"accounts": [account.model_dump() for account in store.list_accounts()]}

    @app.post("/admin/api/accounts", dependencies=[Depends(admin_auth)])
    async def create_account(body: AccountCreate):
        account = store.create_account(body.name, body.account_id, body.cookie_header)
        return account.model_dump()

    @app.patch("/admin/api/accounts/{account_id}", dependencies=[Depends(admin_auth)])
    async def update_account(account_id: str, body: AccountUpdate):
        fields = body.model_dump(exclude_unset=True)
        account = store.update_account(account_id, **fields)
        if not account:
            raise HTTPException(status_code=404, detail="account_not_found")
        return account.model_dump()

    @app.post("/admin/api/accounts/{account_id}/login-session", dependencies=[Depends(admin_auth)])
    async def create_login_session(account_id: str):
        try:
            return browser_kernel.create_interactive_login_session(account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.get("/admin/api/accounts/{account_id}/browser/status", dependencies=[Depends(admin_auth)])
    async def browser_status(account_id: str):
        try:
            return browser_kernel.browser_status(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.post("/admin/api/accounts/{account_id}/browser/start", dependencies=[Depends(admin_auth)])
    async def browser_start(account_id: str):
        try:
            return browser_kernel.start_browser(account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.post("/admin/api/accounts/{account_id}/browser/stop", dependencies=[Depends(admin_auth)])
    async def browser_stop(account_id: str):
        try:
            return browser_kernel.stop_browser(account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.post("/admin/api/accounts/{account_id}/browser/recreate", dependencies=[Depends(admin_auth)])
    async def browser_recreate(account_id: str):
        try:
            return browser_kernel.recreate_browser(account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.post("/admin/api/accounts/{account_id}/validate", dependencies=[Depends(admin_auth)])
    async def validate_account(account_id: str):
        try:
            return await browser_kernel.validate_account(account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="account_not_found") from None

    @app.get("/admin/api/tasks", dependencies=[Depends(admin_auth)])
    async def admin_tasks(
        limit: int = 50,
        account_id: str | None = None,
        kind: str | None = None,
    ):
        return {
            "tasks": [
                task.model_dump()
                for task in store.list_tasks(limit=limit, account_id=account_id, kind=kind)
            ]
        }

    @app.get("/admin/api/tasks/{task_id}", dependencies=[Depends(admin_auth)])
    async def admin_task(task_id: str):
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task_not_found")
        return task.model_dump()

    @app.get("/admin/api/metrics", dependencies=[Depends(admin_auth)])
    async def admin_metrics(since: int | None = None):
        return {"accounts": store.account_metrics(since=since)}

    @app.get("/admin/api/quotas", dependencies=[Depends(admin_auth)])
    async def admin_quotas(account_id: str | None = None):
        return {"quotas": store.list_generation_quotas(account_id=account_id)}

    @app.patch("/admin/api/accounts/{account_id}/quotas/{kind}", dependencies=[Depends(admin_auth)])
    async def admin_update_quota(account_id: str, kind: str, body: GenerationQuotaUpdate):
        if kind not in {"image", "video"}:
            raise HTTPException(status_code=400, detail="invalid_quota_kind")
        if not store.get(account_id):
            raise HTTPException(status_code=404, detail="account_not_found")
        return store.update_generation_quota(
            account_id,
            kind,
            **body.model_dump(exclude_unset=True),
        )

    @app.get("/v1/models", dependencies=[Depends(api_auth)])
    async def models():
        return {"object": "list", "data": MODEL_SPECS}

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(api_auth)])
    async def api_task(task_id: str):
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task_not_found")
        return task.model_dump()

    @app.get("/v1/files/{file_id}")
    async def file_content(file_id: str):
        try:
            path = media_store.path_for(file_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_file_id") from None
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="file_not_found")
        return FileResponse(path)

    @app.post("/v1/chat/completions", dependencies=[Depends(api_auth)])
    async def chat_completions(body: ChatCompletionRequest):
        try:
            result = await browser_kernel.chat_completion(body, body.account_id)
        except BrowserAdapterError as exc:
            raise _adapter_http_error(exc) from None
        content, task_id, account_id = _chat_content(result)
        created = int(time.time())
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        if body.stream:
            return StreamingResponse(
                _stream_chat_response(
                    chat_id=chat_id,
                    created=created,
                    model=body.model,
                    content=content,
                ),
                media_type="text/event-stream",
            )
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": body.model,
            "task_id": task_id,
            "account_id": account_id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @app.post("/v1/responses", dependencies=[Depends(api_auth)])
    async def responses(body: ResponsesRequest):
        chat_body = ChatCompletionRequest(
            model=body.model,
            messages=_responses_messages(body),
            stream=False,
            account_id=body.account_id,
            temperature=body.temperature,
            max_tokens=body.max_output_tokens,
        )
        try:
            result = await browser_kernel.chat_completion(chat_body, body.account_id)
        except BrowserAdapterError as exc:
            raise _adapter_http_error(exc) from None
        content, task_id, account_id = _chat_content(result)
        response = _response_object(
            response_id=f"resp-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=body.model,
            content=content,
            task_id=task_id,
            account_id=account_id,
        )
        if body.stream:
            return StreamingResponse(_stream_responses_response(response), media_type="text/event-stream")
        return response

    async def _image_generation_response(body: ImageGenerationRequest, request: Request):
        try:
            result = await browser_kernel.image_generation(body, body.account_id)
        except BrowserAdapterError as exc:
            raise _adapter_http_error(exc) from None
        raw_data, task_id, account_id = _image_data(result)
        if body.response_format == "b64_json":
            data = [
                {"b64_json": item["b64_json"]}
                for item in raw_data
                if item.get("b64_json")
            ]
        else:
            base_url = str(request.base_url).rstrip("/")
            data = []
            for item in raw_data:
                if item.get("b64_json"):
                    saved = media_store.save_b64(
                        item["b64_json"],
                        media_type=item.get("media_type") or "image/png",
                        prefix="image",
                    )
                    data.append({"url": base_url + saved["url_path"]})
                elif item.get("url"):
                    data.append({"url": item["url"]})
        if body.n > 0:
            data = data[: body.n]
        if not data:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "generated_media_missing",
                    "message": "The upstream page completed without a downloadable image result.",
                },
            )
        return {
            "created": int(time.time()),
            "task_id": task_id,
            "account_id": account_id,
            "data": data,
        }

    @app.post("/v1/images/generations", dependencies=[Depends(api_auth)])
    async def image_generations(request: Request):
        body = await _image_request_from_request(request, ImageGenerationRequest)
        return await _image_generation_response(body, request)

    @app.post("/v1/images/edits", dependencies=[Depends(api_auth)])
    async def image_edits(request: Request):
        body = await _image_request_from_request(request, ImageEditRequest)
        return await _image_generation_response(body, request)

    @app.post("/v1/images/variations", dependencies=[Depends(api_auth)])
    async def image_variations(request: Request):
        body = await _image_request_from_request(
            request,
            ImageVariationRequest,
            default_prompt="Create image variations from the provided reference image.",
        )
        return await _image_generation_response(body, request)

    @app.post("/v1/video/generations", dependencies=[Depends(api_auth)])
    @app.post("/v1/videos", dependencies=[Depends(api_auth)])
    @app.post("/v1/videos/generations", dependencies=[Depends(api_auth)])
    async def video_generations(request: Request):
        body = await _video_request_from_request(request)
        try:
            result = await browser_kernel.video_generation(body, body.account_id)
        except BrowserAdapterError as exc:
            raise _adapter_http_error(exc) from None
        base_url = str(request.base_url).rstrip("/")
        videos = []
        for item in result.get("videos", []):
            if item.get("b64_json"):
                saved = media_store.save_b64(
                    item["b64_json"],
                    media_type=item.get("media_type") or "video/mp4",
                    prefix="video",
                )
                videos.append({"url": base_url + saved["url_path"], "media_type": saved["media_type"]})
            elif item.get("url"):
                videos.append({"url": item["url"]})
        if not videos:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "generated_media_missing",
                    "message": "The upstream page completed without a downloadable video result.",
                },
            )
        return {
            "id": result.get("task_id") or f"video-{uuid.uuid4().hex}",
            "object": "video",
            "created_at": int(time.time()),
            "model": body.model,
            "prompt": body.prompt,
            "task_id": result.get("task_id"),
            "account_id": result.get("account_id"),
            "status": result.get("status", "completed"),
            "video_url": videos[0]["url"],
            "videos": videos,
        }

    return app


app = create_app()
