from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from .browser_adapter import BrowserAdapterError, GrokBrowserAdapter
from .config import settings
from .cookies import cookie_dict_to_playwright, required_grok_cookie_score
from .models import Account, ChatCompletionRequest, ImageGenerationRequest, VideoGenerationRequest
from .store import AccountStore

try:
    import docker
    from docker.errors import DockerException, ImageNotFound, NotFound
except Exception:  # pragma: no cover - docker is optional outside deployment.
    docker = None  # type: ignore[assignment]
    DockerException = ImageNotFound = NotFound = Exception  # type: ignore[misc,assignment]


class BrowserKernel:
    """Account-scoped Chromium profile controller.

    The first implementation intentionally separates two concerns:
    - interactive browser access is provided by an external noVNC/KasmVNC/jlesage
      Chromium container;
    - API automation can later attach to the same account profile through
      Playwright/CDP.

    Keeping this layer small makes it possible to swap jlesage, KasmVNC, or a
    custom Xvfb/noVNC container without changing the public API.
    """

    def __init__(self, store: AccountStore):
        self.store = store
        self._account_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._account_locks:
            self._account_locks[account_id] = asyncio.Lock()
        return self._account_locks[account_id]

    def profile_dir(self, account: Account) -> Path:
        path = Path(account.user_data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def host_profile_dir(self, account: Account) -> Path:
        path = settings.host_data_dir / "profiles" / account.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def browser_public_url(self, account: Account) -> str:
        parsed = urlparse(settings.browser_base_url)
        port = account.browser_port or parsed.port or settings.browser_port_base
        netloc = parsed.hostname or "127.0.0.1"
        if parsed.username or parsed.password:
            netloc = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
        return urlunparse((parsed.scheme or "http", f"{netloc}:{port}", "", "", "", ""))

    def browser_debug_url(self, account: Account) -> str:
        host = settings.docker_host_gateway or "127.0.0.1"
        port = account.browser_debug_port or settings.browser_debug_port_base
        return f"http://{host}:{port}"

    def browser_debug_candidates(self, account: Account) -> list[str]:
        port = account.browser_debug_port or settings.browser_debug_port_base
        candidates = [
            self.browser_debug_url(account),
            f"http://127.0.0.1:{port}",
            f"http://172.17.0.1:{port}",
        ]
        if settings.browser_mode == "docker-novnc":
            try:
                client = self._docker_client()
                container = client.containers.get(account.browser_container)
                networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                for network in networks.values():
                    ip = network.get("IPAddress")
                    if ip:
                        candidates.append(f"http://{ip}:9222")
            except Exception:
                pass
        deduped: list[str] = []
        for item in candidates:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _docker_client(self):
        if docker is None:
            raise RuntimeError("docker_sdk_not_installed")
        return docker.from_env()

    def browser_image_status(self) -> dict[str, Any]:
        result = {
            "mode": settings.browser_mode,
            "image": settings.browser_image,
            "available": False,
            "error": "",
        }
        if settings.browser_mode != "docker-novnc":
            result["available"] = True
            return result
        try:
            client = self._docker_client()
            image = client.images.get(settings.browser_image)
            result["available"] = True
            result["id"] = image.short_id
            result["tags"] = image.tags
        except ImageNotFound:
            result["error"] = "image_not_found"
        except DockerException as exc:
            result["error"] = str(exc)
        return result

    def browser_status(self, account_id: str) -> dict[str, Any]:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        base = {
            "account_id": account.id,
            "mode": settings.browser_mode,
            "image": settings.browser_image,
            "container": account.browser_container,
            "browser_url": self.browser_public_url(account),
            "browser_password": account.browser_password,
            "debug_url": self.browser_debug_url(account),
            "debug_candidates": self.browser_debug_candidates(account),
            "running": False,
            "status": "unknown",
            "error": "",
        }
        if settings.browser_mode != "docker-novnc":
            base["status"] = "external"
            return base
        try:
            client = self._docker_client()
            container = client.containers.get(account.browser_container)
            container.reload()
            base["running"] = container.status == "running"
            base["status"] = container.status
        except NotFound:
            base["status"] = "not_created"
        except DockerException as exc:
            base["status"] = "docker_error"
            base["error"] = str(exc)
        return base

    def start_browser(self, account_id: str) -> dict[str, Any]:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        self.profile_dir(account)
        host_profile = self.host_profile_dir(account)
        if settings.browser_mode != "docker-novnc":
            return self.browser_status(account_id)
        try:
            client = self._docker_client()
            try:
                client.images.get(settings.browser_image)
            except ImageNotFound as exc:
                raise RuntimeError(f"browser_image_missing:{settings.browser_image}") from exc
            try:
                container = client.containers.get(account.browser_container)
                container.reload()
                if container.status != "running":
                    container.start()
            except NotFound:
                environment = {
                    "VNC_PASSWORD": account.browser_password,
                    "DISPLAY_WIDTH": "1440",
                    "DISPLAY_HEIGHT": "900",
                    "START_URL": "https://grok.com/",
                    "CHROME_DEBUG_PORT": "9222",
                    "CHROME_DEBUG_PORT_INTERNAL": "9223",
                    "TZ": settings.browser_timezone,
                }
                if settings.browser_proxy_server:
                    environment["CHROME_PROXY_SERVER"] = settings.browser_proxy_server
                if settings.browser_proxy_bypass_list:
                    environment["CHROME_PROXY_BYPASS_LIST"] = settings.browser_proxy_bypass_list
                client.containers.run(
                    settings.browser_image,
                    detach=True,
                    name=account.browser_container,
                    restart_policy={"Name": "unless-stopped"},
                    environment=environment,
                    volumes={
                        str(host_profile): {"bind": "/config", "mode": "rw"},
                    },
                    ports={
                        "5800/tcp": account.browser_port,
                        "9222/tcp": account.browser_debug_port,
                    },
                    extra_hosts={"host.docker.internal": "host-gateway"},
                    security_opt=["seccomp=unconfined"],
                    shm_size="1g",
                )
        except RuntimeError:
            raise
        except DockerException as exc:
            raise RuntimeError(f"docker_error:{exc}") from exc
        return self.browser_status(account_id)

    def recreate_browser(self, account_id: str) -> dict[str, Any]:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        if settings.browser_mode == "docker-novnc":
            try:
                client = self._docker_client()
                try:
                    container = client.containers.get(account.browser_container)
                    container.remove(force=True)
                except NotFound:
                    pass
            except DockerException as exc:
                raise RuntimeError(f"docker_error:{exc}") from exc
        return self.start_browser(account_id)

    def stop_browser(self, account_id: str) -> dict[str, Any]:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        if settings.browser_mode == "docker-novnc":
            try:
                client = self._docker_client()
                container = client.containers.get(account.browser_container)
                container.stop(timeout=10)
            except NotFound:
                pass
            except DockerException as exc:
                raise RuntimeError(f"docker_error:{exc}") from exc
        return self.browser_status(account_id)

    def create_interactive_login_session(self, account_id: str) -> dict:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        self.profile_dir(account)
        browser = self.start_browser(account_id)
        token = "login-" + secrets.token_urlsafe(24)
        params = urlencode({"account": account.id, "token": token})
        browser_url = f"{browser['browser_url'].rstrip('/')}/vnc.html?autoconnect=1&resize=remote&{params}"
        session = self.store.create_login_session(
            account_id=account.id,
            token=token,
            browser_url=browser_url,
            ttl=settings.session_ttl_seconds,
        )
        session["browser_password"] = account.browser_password
        session["container"] = account.browser_container
        session["debug_url"] = browser["debug_url"]
        return session

    async def inject_cookies_hint(self, account_id: str) -> dict:
        """Return the material needed by a Playwright worker to inject cookies.

        Actual injection is intentionally left to the worker container because
        the remote browser may run in another container namespace. The API is
        still useful for the Admin UI and future orchestrator.
        """
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        cookies = self.store.account_cookies(account_id)
        return {
            "account_id": account_id,
            "profile_dir": str(self.profile_dir(account)),
            "cookie_score": required_grok_cookie_score(cookies),
            "playwright_cookies": cookie_dict_to_playwright(cookies),
        }

    async def validate_account(self, account_id: str) -> dict:
        """Validate the account by attaching to its real browser profile."""
        account = self.store.get(account_id)
        if not account:
            raise KeyError(f"Account not found: {account_id}")
        cookies = self.store.account_cookies(account_id)
        score = required_grok_cookie_score(cookies)
        browser = self.browser_status(account_id)
        debug_url = browser.get("debug_url", "")
        chrome_probe = await self._first_reachable_debug_url(browser.get("debug_candidates") or [debug_url])
        chrome_ok = bool(chrome_probe.get("ok"))
        chrome_error = chrome_probe.get("error", "")
        if chrome_probe.get("url"):
            debug_url = str(chrome_probe["url"])
            browser["debug_url"] = debug_url
        if not browser.get("running") or not chrome_ok:
            status = "login_required"
            error = chrome_error or browser.get("error") or "Remote Chromium is not running."
            self.store.update_account(account_id, status=status, last_error=error)
            return {
                "account_id": account_id,
                "status": status,
                "cookie_score": score,
                "browser": browser,
                "chrome_debug_ok": chrome_ok,
                "chrome_probe": chrome_probe,
                "needs_interactive_login": True,
                "message": error,
            }

        async with self._lock_for(account_id):
            try:
                async with GrokBrowserAdapter(account, debug_url) as adapter:
                    result = await adapter.validate()
                    await self._writeback_cookies(account_id, adapter)
            except BrowserAdapterError as exc:
                status = "blocked" if exc.code == "playwright_missing" else "error"
                self.store.update_account(account_id, status=status, last_error=exc.message)
                return {
                    "account_id": account_id,
                    "status": status,
                    "cookie_score": score,
                    "browser": browser,
                    "chrome_debug_ok": chrome_ok,
                    "chrome_probe": chrome_probe,
                    "needs_interactive_login": True,
                    "message": exc.message,
                    "error": exc.payload(),
                }
            except Exception as exc:
                message = str(exc)
                self.store.update_account(account_id, status="error", last_error=message)
                return {
                    "account_id": account_id,
                    "status": "error",
                    "cookie_score": score,
                    "browser": browser,
                    "chrome_debug_ok": chrome_ok,
                    "chrome_probe": chrome_probe,
                    "needs_interactive_login": True,
                    "message": message,
                    "error": {"error": "adapter_unexpected_error", "message": message},
                }

        status = result.status
        self.store.update_account(
            account_id,
            status=status,
            capabilities=result.capabilities if status == "ready" else [],
            last_validated_at=int(time.time()) if status == "ready" else None,
            last_error="" if status == "ready" else result.message,
        )
        return {
            "account_id": account_id,
            "status": status,
            "cookie_score": score,
            "browser": browser,
            "chrome_debug_ok": chrome_ok,
            "chrome_probe": chrome_probe,
            "needs_interactive_login": status != "ready",
            "message": result.message,
            "validation": result.model_dump(),
        }

    def ready_accounts(self) -> list[Account]:
        return [
            account
            for account in self.store.list_accounts()
            if account.enabled and account.status == "ready"
        ]

    def pick_ready_account(self, preferred_account_id: str | None = None) -> Account:
        if preferred_account_id:
            account = self.store.get(preferred_account_id)
            if not account:
                raise BrowserAdapterError("account_not_found", "Requested account was not found.", 404)
            if not account.enabled:
                raise BrowserAdapterError("account_disabled", "Requested account is disabled.", 409)
            if account.status != "ready":
                raise BrowserAdapterError(
                    "provider_login_required",
                    "Requested account is not ready. Start the browser, finish Grok login, then validate it.",
                    409,
                    {"account_id": account.id, "status": account.status},
                )
            return account

        accounts = self.ready_accounts()
        if not accounts:
            raise BrowserAdapterError(
                "provider_login_required",
                "No ready Grok Web account is available. Add an account, open the remote browser, log in to Grok, then validate.",
                409,
            )
        recent = self.store.account_metrics(since=int(time.time()) - 24 * 60 * 60)
        accounts.sort(
            key=lambda item: (
                recent.get(item.id, {}).get("by_status", {}).get("failed", 0),
                recent.get(item.id, {}).get("by_status", {}).get("running", 0),
                item.last_validated_at or item.updated_at or item.created_at,
            )
        )
        return accounts[0]

    async def chat_completion(self, body: ChatCompletionRequest, account_id: str | None = None) -> dict[str, Any]:
        task = self.store.create_task(
            kind="chat",
            model=body.model,
            prompt=self._messages_prompt(body.messages),
            account_id=account_id,
            request=self._safe_request_payload(body.model_dump()),
        )
        account: Account | None = None
        try:
            account = self.pick_ready_account(account_id)
            self.store.update_task(task.task_id, account_id=account.id, status="running")
            browser = self.browser_status(account.id)
            if not browser.get("running"):
                raise BrowserAdapterError("browser_not_running", "Ready account browser is not running.", 503)
            debug_url = await self._require_debug_url(browser)
            async with self._lock_for(account.id):
                async with GrokBrowserAdapter(account, debug_url) as adapter:
                    content = await adapter.chat(body.messages)
                    await self._writeback_cookies(account.id, adapter)
            result = {"content": content}
            self.store.update_task(task.task_id, status="completed", result=result)
            return {"task_id": task.task_id, "account_id": account.id, **result}
        except BrowserAdapterError as exc:
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=exc.message,
                result=exc.payload(),
            )
            raise
        except Exception as exc:
            message = str(exc)
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=message,
                result={"error": "adapter_unexpected_error", "message": message},
            )
            raise BrowserAdapterError("adapter_unexpected_error", message) from exc

    async def image_generation(
        self,
        body: ImageGenerationRequest,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.create_task(
            kind="image",
            model=body.model,
            prompt=body.prompt,
            account_id=account_id,
            request=self._safe_request_payload(body.model_dump()),
        )
        account: Account | None = None
        try:
            account = self.pick_ready_account(account_id)
            self.store.update_task(task.task_id, account_id=account.id, status="running")
            browser = self.browser_status(account.id)
            if not browser.get("running"):
                raise BrowserAdapterError("browser_not_running", "Ready account browser is not running.", 503)
            debug_url = await self._require_debug_url(browser)
            async with self._lock_for(account.id):
                async with GrokBrowserAdapter(account, debug_url) as adapter:
                    data = await adapter.generate_image(
                        body.prompt,
                        response_format="b64_json",
                        n=body.n,
                        size=body.size,
                        images=body.image,
                    )
                    await self._writeback_cookies(account.id, adapter)
            result = {"data": data}
            self.store.update_task(task.task_id, status="completed", result=result)
            return {"task_id": task.task_id, "account_id": account.id, **result}
        except BrowserAdapterError as exc:
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=exc.message,
                result=exc.payload(),
            )
            raise
        except Exception as exc:
            message = str(exc)
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=message,
                result={"error": "adapter_unexpected_error", "message": message},
            )
            raise BrowserAdapterError("adapter_unexpected_error", message) from exc

    async def video_generation(
        self,
        body: VideoGenerationRequest,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.create_task(
            kind="video",
            model=body.model,
            prompt=body.prompt,
            account_id=account_id,
            request=self._safe_request_payload(body.model_dump()),
        )
        account: Account | None = None
        try:
            account = self.pick_ready_account(account_id)
            self.store.update_task(task.task_id, account_id=account.id, status="running")
            browser = self.browser_status(account.id)
            if not browser.get("running"):
                raise BrowserAdapterError("browser_not_running", "Ready account browser is not running.", 503)
            debug_url = await self._require_debug_url(browser)
            async with self._lock_for(account.id):
                async with GrokBrowserAdapter(account, debug_url) as adapter:
                    result = await adapter.generate_video_with_options(
                        body.prompt,
                        duration=body.duration,
                        aspect_ratio=body.aspect_ratio,
                        size=body.size,
                        images=body.image,
                    )
                    await self._writeback_cookies(account.id, adapter)
            self.store.update_task(task.task_id, status="completed", result=result)
            return {"task_id": task.task_id, "account_id": account.id, **result}
        except BrowserAdapterError as exc:
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=exc.message,
                result=exc.payload(),
            )
            raise
        except Exception as exc:
            message = str(exc)
            self.store.update_task(
                task.task_id,
                account_id=account.id if account else account_id,
                status="failed",
                error=message,
                result={"error": "adapter_unexpected_error", "message": message},
            )
            raise BrowserAdapterError("adapter_unexpected_error", message) from exc

    async def _first_reachable_debug_url(self, urls: list[str]) -> dict[str, Any]:
        probes = []
        async with httpx.AsyncClient(timeout=3) as client:
            for url in urls:
                clean = str(url).rstrip("/")
                try:
                    response = await client.get(f"{clean}/json/version")
                    probe = {
                        "url": clean,
                        "ok": response.status_code == 200,
                        "status_code": response.status_code,
                    }
                    if response.status_code == 200:
                        return {**probe, "probes": probes + [probe]}
                    probes.append(probe)
                except Exception as exc:
                    probes.append({"url": clean, "ok": False, "error": str(exc)})
        error = probes[-1].get("error") if probes else "no_debug_candidates"
        return {"ok": False, "error": error, "probes": probes}

    async def _require_debug_url(self, browser: dict[str, Any]) -> str:
        probe = await self._first_reachable_debug_url(
            list(browser.get("debug_candidates") or [browser.get("debug_url", "")])
        )
        if not probe.get("ok"):
            raise BrowserAdapterError(
                "chrome_debug_unreachable",
                "Account browser is running, but Chrome DevTools is not reachable.",
                503,
                {"chrome_probe": probe},
            )
        return str(probe["url"])

    async def _writeback_cookies(self, account_id: str, adapter: GrokBrowserAdapter) -> None:
        if not settings.cookie_writeback:
            return
        cookies = await adapter.export_grok_cookies()
        if cookies:
            self.store.update_account(account_id, cookie_json=json.dumps(cookies, ensure_ascii=False))

    @staticmethod
    def _messages_prompt(messages: list[Any]) -> str:
        parts = []
        for message in messages:
            role = getattr(message, "role", "")
            content = getattr(message, "content", "")
            if isinstance(content, str):
                text = content
            else:
                chunks = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("text") or item.get("content"):
                        chunks.append(str(item.get("text") or item.get("content")))
                    elif any(key in item for key in ("image_url", "input_image", "video_url", "file")):
                        chunks.append("[media]")
                text = " ".join(chunks)
            if text.strip():
                parts.append(f"{role}: {text.strip()}")
        return "\n".join(parts)

    @staticmethod
    def _safe_request_payload(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: BrowserKernel._safe_request_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [BrowserKernel._safe_request_payload(item) for item in value]
        if isinstance(value, str):
            if value.startswith("data:"):
                return value.split(",", 1)[0] + ",<redacted>"
            if len(value) > 1000:
                return value[:1000] + "...<truncated>"
        return value
