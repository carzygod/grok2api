from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings
from .models import Account, ChatMessage


APP_URL = "https://grok.com/"
IMAGES_URL = "https://grok.com/imagine"


class BrowserAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            **({"details": self.details} if self.details else {}),
        }


@dataclass
class BrowserValidation:
    status: str
    message: str
    page_url: str
    title: str
    input_ready: bool
    image_page_ready: bool
    login_detected: bool
    capabilities: list[str]

    def model_dump(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "page_url": self.page_url,
            "title": self.title,
            "input_ready": self.input_ready,
            "image_page_ready": self.image_page_ready,
            "login_detected": self.login_detected,
            "capabilities": self.capabilities,
        }


class GrokBrowserAdapter:
    """Drive the already-running account Chromium through CDP.

    The API container does not own the browser process. It connects to the
    account's remote debugging endpoint, operates the first page, then drops the
    CDP connection. The persistent profile remains in the browser container.
    """

    def __init__(self, account: Account, debug_url: str):
        self.account = account
        self.debug_url = debug_url.rstrip("/")
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._trace_active = False

    async def __aenter__(self) -> "GrokBrowserAdapter":
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - depends on deployment image.
            raise BrowserAdapterError(
                "playwright_missing",
                "Playwright is not installed in the API container. Rebuild grok2api with the updated requirements.",
                details={"import_error": str(exc)},
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.debug_url)
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise BrowserAdapterError(
                "cdp_connect_failed",
                f"Cannot connect to account browser CDP endpoint: {self.debug_url}",
                details={"error": str(exc)},
            ) from exc
        contexts = self._browser.contexts
        context = contexts[0] if contexts else await self._browser.new_context()
        self._context = context
        if settings.capture_traces:
            try:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)
                self._trace_active = True
            except Exception:
                self._trace_active = False
        self._page = context.pages[0] if context.pages else await context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            diagnostics = await self.capture_diagnostics(
                getattr(exc, "code", exc_type.__name__ if exc_type else "error")
            )
            if isinstance(exc, BrowserAdapterError) and diagnostics:
                exc.details["diagnostics"] = diagnostics
        # Do not call browser.close(); with CDP that may close the remote browser.
        if self._playwright:
            await self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._trace_active = False

    @property
    def page(self):
        if self._page is None:
            raise BrowserAdapterError("browser_page_missing", "Browser page is not available.")
        return self._page

    async def validate(self) -> BrowserValidation:
        await self._goto(APP_URL, timeout_ms=60_000)
        await self._settle(2)
        login_detected = await self._login_detected()
        input_ready = False if login_detected else await self._input_ready()
        title = await self._safe_title()
        page_url = self.page.url
        image_page_ready = False

        if input_ready:
            try:
                await self._goto(IMAGES_URL, timeout_ms=60_000)
                await self._settle(2)
                image_page_ready = not await self._login_detected()
            except Exception:
                image_page_ready = False
            await self._goto(APP_URL, timeout_ms=60_000)
            await self._settle(1)

        if login_detected:
            return BrowserValidation(
                status="login_required",
                message="Grok/X login page is visible in the account browser.",
                page_url=page_url,
                title=title,
                input_ready=False,
                image_page_ready=False,
                login_detected=True,
                capabilities=[],
            )
        if not input_ready:
            return BrowserValidation(
                status="checking",
                message="Grok page loaded, but the prompt input was not found yet.",
                page_url=page_url,
                title=title,
                input_ready=False,
                image_page_ready=image_page_ready,
                login_detected=False,
                capabilities=[],
            )
        capabilities = ["chat"]
        if image_page_ready:
            capabilities.append("image")
        if await self._video_capability_hint():
            capabilities.append("video")
        return BrowserValidation(
            status="ready",
            message="Grok page and prompt input are reachable from the account browser.",
            page_url=page_url,
            title=title,
            input_ready=True,
            image_page_ready=image_page_ready,
            login_detected=False,
            capabilities=capabilities,
        )

    async def chat(self, messages: list[ChatMessage], timeout_s: int = 150) -> str:
        prompt = self._messages_to_prompt(messages)
        if not prompt:
            raise BrowserAdapterError("empty_prompt", "No user prompt was supplied.", status_code=400)
        await self._goto(APP_URL, timeout_ms=60_000)
        await self._settle(2)
        await self._require_logged_in()
        before = await self._body_text()
        await self._submit_prompt(prompt)
        answer = await self._wait_for_text_delta(before, prompt, timeout_s=timeout_s)
        if not answer:
            raise BrowserAdapterError(
                "empty_model_response",
                "Grok did not produce a readable text response before timeout.",
            )
        return answer

    async def generate_image(
        self,
        prompt: str,
        *,
        response_format: str | None = None,
        n: int = 1,
        size: str | None = None,
        images: str | list[str] | None = None,
        timeout_s: int = 180,
    ) -> list[dict[str, str]]:
        if not prompt:
            raise BrowserAdapterError("empty_prompt", "Image prompt is required.", status_code=400)
        await self._goto(IMAGES_URL, timeout_ms=60_000)
        await self._settle(2)
        await self._require_logged_in()
        upload_files, prompt_refs = self._materialize_input_media(images, prefix="image-input")
        await self._attach_input_files(upload_files)
        final_prompt = self._generation_prompt(
            prompt,
            count=n,
            size=size,
            references=prompt_refs,
        )
        seen = await self._image_sources()
        await self._submit_prompt(final_prompt)
        images = await self._wait_for_new_images(seen, timeout_s=timeout_s)
        if not images:
            raise BrowserAdapterError(
                "image_generation_timeout",
                "No new generated image was found in the Grok Images page before timeout.",
            )
        if (response_format or "url") == "b64_json":
            out = []
            for item in images:
                material = item if item.get("b64_json") else await self._url_to_b64(
                    item.get("url", ""),
                    default_media_type="image/png",
                )
                b64 = material.get("b64_json", "")
                if b64:
                    out.append({
                        "b64_json": b64,
                        "media_type": material.get("media_type") or item.get("media_type") or "image/png",
                    })
            if not out:
                raise BrowserAdapterError(
                    "media_fetch_failed",
                    "Generated image nodes were found, but none could be fetched from the browser context.",
                    details={"count": len(images)},
                )
            return out
        return [{"url": item["url"]} for item in images if item.get("url")]

    async def generate_video(self, prompt: str, *, timeout_s: int = 300) -> dict[str, Any]:
        if not prompt:
            raise BrowserAdapterError("empty_prompt", "Video prompt is required.", status_code=400)
        await self._goto(IMAGES_URL, timeout_ms=60_000)
        await self._settle(2)
        await self._require_logged_in()
        upload_files, prompt_refs = self._materialize_input_media(None, prefix="video-input")
        seen = await self._video_sources()
        await self._attach_input_files(upload_files)
        await self._submit_prompt(self._generation_prompt(prompt, references=prompt_refs))
        videos = await self._wait_for_new_videos(seen, timeout_s=timeout_s)
        if not videos:
            raise BrowserAdapterError(
                "video_generation_timeout",
                "No generated video was found in the Grok page before timeout.",
            )
        out = []
        for url in videos:
            material = await self._url_to_b64(url, default_media_type="video/mp4")
            if material.get("b64_json"):
                out.append(material)
            elif url and not url.startswith("blob:"):
                out.append({"url": url})
        if not out:
            raise BrowserAdapterError(
                "media_fetch_failed",
                "Generated video nodes were found, but none could be fetched from the browser context.",
                details={"count": len(videos)},
            )
        return {
            "status": "completed",
            "videos": out,
        }

    async def generate_video_with_options(
        self,
        prompt: str,
        *,
        duration: int | None = None,
        aspect_ratio: str | None = None,
        size: str | None = None,
        images: str | list[str] | None = None,
        timeout_s: int = 300,
    ) -> dict[str, Any]:
        if not prompt:
            raise BrowserAdapterError("empty_prompt", "Video prompt is required.", status_code=400)
        await self._goto(IMAGES_URL, timeout_ms=60_000)
        await self._settle(2)
        await self._require_logged_in()
        upload_files, prompt_refs = self._materialize_input_media(images, prefix="video-input")
        await self._attach_input_files(upload_files)
        seen = await self._video_sources()
        final_prompt = self._generation_prompt(
            prompt,
            duration=duration,
            aspect_ratio=aspect_ratio,
            size=size,
            references=prompt_refs,
        )
        await self._submit_prompt(final_prompt)
        videos = await self._wait_for_new_videos(seen, timeout_s=timeout_s)
        if not videos:
            raise BrowserAdapterError(
                "video_generation_timeout",
                "No generated video was found in the Grok page before timeout.",
            )
        out = []
        for url in videos:
            material = await self._url_to_b64(url, default_media_type="video/mp4")
            if material.get("b64_json"):
                out.append(material)
            elif url and not url.startswith("blob:"):
                out.append({"url": url})
        if not out:
            raise BrowserAdapterError(
                "media_fetch_failed",
                "Generated video nodes were found, but none could be fetched from the browser context.",
                details={"count": len(videos)},
            )
        return {
            "status": "completed",
            "videos": out,
        }

    async def export_grok_cookies(self) -> dict[str, str]:
        if self._context is None:
            return {}
        try:
            cookies = await self._context.cookies(
                ["https://grok.com", "https://x.com", "https://twitter.com"]
            )
        except Exception:
            return {}
        return {
            item.get("name", ""): item.get("value", "")
            for item in cookies
            if item.get("name") and item.get("value")
        }

    async def capture_diagnostics(self, label: str) -> dict[str, str]:
        if self._page is None and self._context is None:
            return {}
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label or "error").strip("-") or "error"
        root = settings.diagnostics_dir / self.account.id
        root.mkdir(parents=True, exist_ok=True)
        stem = f"{int(time.time())}-{safe_label}"
        result: dict[str, str] = {}
        if self._page is not None:
            screenshot_path = root / f"{stem}.png"
            html_path = root / f"{stem}.html"
            try:
                await self._page.screenshot(path=str(screenshot_path), full_page=True, timeout=5_000)
                result["screenshot_path"] = str(screenshot_path)
            except Exception as exc:
                result["screenshot_error"] = str(exc)
            try:
                html_path.write_text(await self._page.content(), encoding="utf-8")
                result["html_path"] = str(html_path)
            except Exception as exc:
                result["html_error"] = str(exc)
            try:
                result["page_url"] = self._page.url
                result["title"] = await self._safe_title()
            except Exception:
                pass
        if self._context is not None and self._trace_active:
            trace_path = root / f"{stem}.zip"
            try:
                await self._context.tracing.stop(path=str(trace_path))
                self._trace_active = False
                result["trace_path"] = str(trace_path)
            except Exception as exc:
                result["trace_error"] = str(exc)
        return result

    async def _goto(self, url: str, *, timeout_ms: int) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            # Chrome can keep the page in a usable state even if a wait condition times out.
            current_url = self.page.url or ""
            if current_url.startswith("https://") or "grok.com" in current_url:
                return
            raise BrowserAdapterError(
                "navigation_failed",
                f"Could not open {url} before timeout.",
                details={"page_url": current_url, "error": str(exc)},
            ) from exc

    async def _settle(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _safe_title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def _body_text(self) -> str:
        try:
            text = await self.page.locator("body").inner_text(timeout=5_000)
            return text or ""
        except Exception:
            return ""

    async def _login_detected(self) -> bool:
        url = self.page.url.lower()
        if "x.com/i/flow/login" in url or "/login" in url or "/signin" in url:
            return True
        text = (await self._body_text()).lower()
        markers = (
            "log in",
            "sign in",
            "sign in with x",
            "continue with x",
            "phone, email, or username",
            "choose an account",
            "create account",
            "forgot password",
        )
        return any(marker in text for marker in markers)

    async def _input_ready(self) -> bool:
        for selector in self._prompt_selectors():
            try:
                if await self.page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _require_logged_in(self) -> None:
        if await self._login_detected():
            raise BrowserAdapterError(
                "provider_login_required",
                "The account browser is showing a Grok/X login page.",
                status_code=409,
                details={"page_url": self.page.url},
            )

    async def _video_capability_hint(self) -> bool:
        text = (await self._body_text()).lower()
        url = self.page.url.lower()
        markers = ("imagine", "video", "animate", "motion")
        return any(marker in text or marker in url for marker in markers)

    @staticmethod
    def _prompt_selectors() -> tuple[str, ...]:
        return (
            "textarea",
            "div[contenteditable='true']",
            "[role='textbox']",
            "[aria-label*='prompt' i]",
            "[aria-label*='message' i]",
        )

    async def _submit_prompt(self, prompt: str) -> None:
        target = None
        for selector in self._prompt_selectors():
            try:
                if await self.page.locator(selector).count() > 0:
                    target = self.page.locator(selector).last
                    break
            except Exception:
                continue
        if target is None:
            raise BrowserAdapterError("prompt_input_not_found", "Prompt input was not found.")

        try:
            await target.click(timeout=10_000)
        except Exception:
            pass

        tag = await target.evaluate("(el) => el.tagName.toLowerCase()")
        if tag in {"textarea", "input"}:
            await target.fill(prompt, timeout=10_000)
        else:
            await target.evaluate(
                """(el, text) => {
                    el.focus();
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    selection.removeAllRanges();
                    selection.addRange(range);
                    document.execCommand('insertText', false, text);
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
                }""",
                prompt,
            )

        clicked = await self._click_send_button()
        if not clicked:
            await target.press("Enter")

    async def _attach_input_files(self, paths: list[str]) -> bool:
        if not paths:
            return False
        selectors = (
            "input[type='file']",
            "input[accept*='image']",
            "input[accept*='video']",
        )
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                if await locator.count() > 0:
                    await locator.last.set_input_files(paths, timeout=10_000)
                    await self._settle(1)
                    return True
            except Exception:
                continue
        return False

    async def _click_send_button(self) -> bool:
        selectors = (
            "button[aria-label*='Send' i]",
            "button[aria-label*='Submit' i]",
            "button:has-text('Send')",
            "button:has-text('Submit')",
        )
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                count = await loc.count()
                for index in range(count - 1, -1, -1):
                    button = loc.nth(index)
                    if await button.is_enabled(timeout=1_000):
                        await button.click(timeout=5_000)
                        return True
            except Exception:
                continue
        return False

    async def _wait_for_text_delta(self, before: str, prompt: str, *, timeout_s: int) -> str:
        deadline = time.monotonic() + timeout_s
        best = ""
        stable_seen = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            text = await self._body_text()
            candidate = self._extract_answer(before, text, prompt)
            if len(candidate) > len(best):
                best = candidate
                stable_seen = 0
            elif best:
                stable_seen += 1
                if stable_seen >= 3:
                    return best.strip()
        return best.strip()

    @staticmethod
    def _extract_answer(before: str, after: str, prompt: str) -> str:
        text = after or ""
        if before and text.startswith(before):
            text = text[len(before) :]
        elif prompt and prompt in text:
            text = text.rsplit(prompt, 1)[-1]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        noisy = {
            "grok",
            "send",
            "new chat",
            "sign in",
            "upgrade",
            "privacy",
            "terms",
        }
        lines = [line for line in lines if line.lower() not in noisy]
        return "\n".join(lines)[-8000:]

    @staticmethod
    def _generation_prompt(
        prompt: str,
        *,
        count: int | None = None,
        duration: int | None = None,
        aspect_ratio: str | None = None,
        size: str | None = None,
        references: list[str] | None = None,
    ) -> str:
        parts = [prompt.strip()]
        constraints = []
        if count and count > 1:
            constraints.append(f"create {count} distinct results")
        if size:
            constraints.append(f"target size/resolution: {size}")
        if duration:
            constraints.append(f"target duration: {duration} seconds")
        if aspect_ratio:
            constraints.append(f"target aspect ratio: {aspect_ratio}")
        if constraints:
            parts.append("Constraints: " + "; ".join(constraints) + ".")
        if references:
            parts.append("Reference media URLs: " + " ".join(references))
        return "\n".join(parts)

    @staticmethod
    def _media_items(value: str | list[str] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [item for item in value if isinstance(item, str) and item.strip()]

    def _materialize_input_media(
        self,
        value: str | list[str] | None,
        *,
        prefix: str,
    ) -> tuple[list[str], list[str]]:
        upload_files: list[str] = []
        references: list[str] = []
        root = settings.downloads_dir / "inputs" / self.account.id
        for item in self._media_items(value):
            text = item.strip()
            if text.startswith("data:"):
                header, _, payload = text.partition(",")
                media_type = header.split(";", 1)[0].removeprefix("data:") or "application/octet-stream"
                ext = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "video/mp4": ".mp4",
                    "video/webm": ".webm",
                }.get(media_type.lower(), ".bin")
                try:
                    raw = base64.b64decode(payload, validate=False)
                except Exception:
                    references.append(text[:200])
                    continue
                root.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(raw).hexdigest()[:16]
                path = root / f"{prefix}-{int(time.time())}-{digest}{ext}"
                path.write_bytes(raw)
                upload_files.append(str(path))
                continue
            path = Path(text)
            if path.exists() and path.is_file():
                upload_files.append(str(path))
            else:
                references.append(text)
        return upload_files, references

    async def _image_sources(self) -> set[str]:
        rows = await self.page.evaluate(
            """() => Array.from(document.images)
                .map(img => img.currentSrc || img.src || '')
                .filter(Boolean)"""
        )
        return set(rows or [])

    async def _wait_for_new_images(self, seen: set[str], *, timeout_s: int) -> list[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            rows = await self.page.evaluate(
                """async (seen) => {
                    const old = new Set(seen || []);
                    const out = [];
                    for (const img of Array.from(document.images)) {
                        const src = img.currentSrc || img.src || '';
                        if (!src || old.has(src)) continue;
                        if ((img.naturalWidth || 0) < 128 || (img.naturalHeight || 0) < 128) continue;
                        out.push({url: src, width: img.naturalWidth || 0, height: img.naturalHeight || 0});
                    }
                    return out;
                }""",
                list(seen),
            )
            if rows:
                return rows
        return []

    async def _url_to_b64(self, url: str, *, default_media_type: str) -> dict[str, str]:
        if not url:
            return {}
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            media_type = header.split(";", 1)[0].removeprefix("data:") or default_media_type
            return {"b64_json": payload, "media_type": media_type} if payload else {}
        result = await self.page.evaluate(
            """async ({url, defaultMediaType}) => {
                try {
                    const res = await fetch(url, {credentials: 'include'});
                    if (!res.ok) return {};
                    const blob = await res.blob();
                    return await new Promise(resolve => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve({
                            b64_json: String(reader.result || '').split(',')[1] || '',
                            media_type: blob.type || defaultMediaType
                        });
                        reader.onerror = () => resolve({});
                        reader.readAsDataURL(blob);
                    });
                } catch (e) {
                    return {};
                }
            }""",
            {"url": url, "defaultMediaType": default_media_type},
        )
        return result or {}

    async def _video_sources(self) -> set[str]:
        rows = await self.page.evaluate(
            """() => Array.from(document.querySelectorAll('video, a'))
                .map(el => el.currentSrc || el.src || el.href || '')
                .filter(src => src && /\\.mp4|video|blob:/i.test(src))"""
        )
        return set(rows or [])

    async def _wait_for_new_videos(self, seen: set[str], *, timeout_s: int) -> list[str]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            rows = await self.page.evaluate(
                """(seen) => {
                    const old = new Set(seen || []);
                    return Array.from(document.querySelectorAll('video, a'))
                        .map(el => el.currentSrc || el.src || el.href || '')
                        .filter(src => src && !old.has(src) && /\\.mp4|video|blob:/i.test(src));
                }""",
                list(seen),
            )
            if rows:
                return rows
        return []

    @staticmethod
    def _messages_to_prompt(messages: list[ChatMessage]) -> str:
        parts = []
        for message in messages:
            if isinstance(message.content, str):
                content = message.content
            else:
                content = "\n".join(
                    str(item.get("text") or item.get("content") or "")
                    for item in message.content
                    if isinstance(item, dict)
                )
            if content.strip():
                parts.append(f"{message.role}: {content.strip()}")
        if not parts:
            return ""
        return "\n".join(parts)


def b64_data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
