from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings
from .models import Account, ChatMessage


APP_URL = "https://grok.com/"
IMAGES_URL = "https://grok.com/imagine"
MAX_INPUT_MEDIA_BYTES = 80 * 1024 * 1024


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
        video_page_ready = False

        if input_ready:
            try:
                await self._goto(IMAGES_URL, timeout_ms=60_000)
                await self._settle(2)
                image_page_ready = not await self._login_detected()
                video_page_ready = image_page_ready and await self._video_capability_hint()
            except Exception:
                image_page_ready = False
                video_page_ready = False
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
        if video_page_ready:
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
        prompt, media_items = self._messages_to_prompt_and_media(messages)
        if not prompt and media_items:
            prompt = "Describe the attached media."
        if not prompt:
            raise BrowserAdapterError("empty_prompt", "No user prompt was supplied.", status_code=400)
        await self._goto(APP_URL, timeout_ms=60_000)
        await self._settle(2)
        await self._require_logged_in()
        upload_files, prompt_refs = await self._materialize_input_media(
            media_items,
            prefix="chat-input",
        )
        attached = await self._attach_input_files(upload_files)
        if upload_files and not attached:
            raise BrowserAdapterError(
                "media_upload_failed",
                "Input media was prepared, but the Grok upload control was not found.",
                details={"count": len(upload_files)},
            )
        if prompt_refs:
            prompt = prompt + "\nReference media URLs: " + " ".join(prompt_refs)
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
        await self._select_imagine_mode("image")
        upload_files, prompt_refs = await self._materialize_input_media(images, prefix="image-input")
        attached = await self._attach_input_files(upload_files)
        if upload_files and not attached:
            raise BrowserAdapterError(
                "media_upload_failed",
                "Reference media was prepared, but the Grok Imagine upload control was not found.",
                details={"count": len(upload_files)},
            )
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
        await self._select_imagine_mode("video")
        upload_files, prompt_refs = await self._materialize_input_media(None, prefix="video-input")
        seen = await self._video_sources()
        attached = await self._attach_input_files(upload_files)
        if upload_files and not attached:
            raise BrowserAdapterError(
                "media_upload_failed",
                "Reference media was prepared, but the Grok Imagine upload control was not found.",
                details={"count": len(upload_files)},
            )
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
        await self._select_imagine_mode("video")
        upload_files, prompt_refs = await self._materialize_input_media(images, prefix="video-input")
        attached = await self._attach_input_files(upload_files)
        if upload_files and not attached:
            raise BrowserAdapterError(
                "media_upload_failed",
                "Reference media was prepared, but the Grok Imagine upload control was not found.",
                details={"count": len(upload_files)},
            )
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
        button_selectors = (
            "button[aria-label*='Attach' i]",
            "button[aria-label*='Upload' i]",
            "button[aria-label*='Add files' i]",
            "button[aria-label*='Add image' i]",
            "button[aria-label*='Image' i]",
            "button:has-text('Attach')",
            "button:has-text('Upload')",
            "button:has-text('Add image')",
            "button:has-text('Image')",
        )
        for selector in button_selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                for index in range(count - 1, -1, -1):
                    button = locator.nth(index)
                    if not await button.is_visible(timeout=500):
                        continue
                    if not await button.is_enabled(timeout=500):
                        continue
                    async with self.page.expect_file_chooser(timeout=3_000) as chooser_info:
                        await button.click(timeout=3_000)
                    chooser = await chooser_info.value
                    await chooser.set_files(paths)
                    await self._settle(1)
                    return True
            except Exception:
                continue
        return False

    async def _select_imagine_mode(self, mode: str) -> bool:
        labels = {
            "image": ("Image", "Images", "Generate images"),
            "video": ("Video", "Videos", "Animate", "Generate videos"),
        }.get(mode, (mode,))
        selectors: list[str] = []
        for label in labels:
            selectors.extend(
                [
                    f"[role='tab']:has-text('{label}')",
                    f"button:has-text('{label}')",
                    f"button[aria-label*='{label}' i]",
                    f"[role='button'][aria-label*='{label}' i]",
                ]
            )
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if not await item.is_visible(timeout=500):
                        continue
                    if not await item.is_enabled(timeout=500):
                        continue
                    await item.click(timeout=3_000)
                    await self._settle(0.8)
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

    async def _materialize_input_media(
        self,
        value: str | list[str] | list[Any] | None,
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
                try:
                    raw = base64.b64decode(payload, validate=False)
                except Exception:
                    references.append(text[:200])
                    continue
                path = self._write_input_media(root, prefix, raw, media_type)
                upload_files.append(str(path))
                continue
            if text.startswith(("http://", "https://")):
                downloaded = await self._download_input_media(text, root=root, prefix=prefix)
                if downloaded:
                    upload_files.append(str(downloaded))
                else:
                    references.append(text)
                continue
            path = Path(text)
            if path.exists() and path.is_file():
                upload_files.append(str(path))
            else:
                references.append(text)
        return upload_files, references

    @classmethod
    def _write_input_media(
        cls,
        root: Path,
        prefix: str,
        raw: bytes,
        media_type: str,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        ext = cls._extension_for_media(media_type)
        path = root / f"{prefix}-{int(time.time())}-{digest}{ext}"
        path.write_bytes(raw)
        return path

    @staticmethod
    def _extension_for_media(media_type: str) -> str:
        known = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }
        return known.get(media_type.lower()) or mimetypes.guess_extension(media_type) or ".bin"

    async def _download_input_media(self, url: str, *, root: Path, prefix: str) -> Path | None:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(url)
            if response.status_code >= 400:
                return None
            raw = response.content
            if not raw or len(raw) > MAX_INPUT_MEDIA_BYTES:
                return None
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if not media_type or media_type == "application/octet-stream":
                guessed, _ = mimetypes.guess_type(urlparse(str(response.url)).path)
                media_type = guessed or media_type or "application/octet-stream"
            return self._write_input_media(root, prefix, raw, media_type)
        except Exception:
            return None

    async def _image_sources(self) -> set[str]:
        rows = await self._media_sources("image")
        return {item.get("url", "") for item in rows if item.get("url")}

    async def _wait_for_new_images(self, seen: set[str], *, timeout_s: int) -> list[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            rows = [
                item
                for item in await self._media_sources("image")
                if item.get("url") and item.get("url") not in seen
            ]
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
        rows = await self._media_sources("video")
        return {item.get("url", "") for item in rows if item.get("url")}

    async def _wait_for_new_videos(self, seen: set[str], *, timeout_s: int) -> list[str]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            rows = [
                item.get("url", "")
                for item in await self._media_sources("video")
                if item.get("url") and item.get("url") not in seen
            ]
            if rows:
                return rows
        return []

    async def _media_sources(self, kind: str) -> list[dict[str, Any]]:
        return await self.page.evaluate(
            """(kind) => {
                const out = [];
                const push = (url, meta = {}) => {
                    if (!url || typeof url !== 'string') return;
                    const clean = url.trim();
                    if (!clean || clean.startsWith('chrome-extension:')) return;
                    out.push({url: clean, ...meta});
                };
                const largestFromSrcset = (srcset) => {
                    if (!srcset) return '';
                    const parts = String(srcset).split(',').map(part => part.trim()).filter(Boolean);
                    if (!parts.length) return '';
                    return parts[parts.length - 1].split(/\\s+/)[0] || '';
                };
                if (kind === 'image') {
                    for (const img of Array.from(document.images)) {
                        const width = img.naturalWidth || img.width || 0;
                        const height = img.naturalHeight || img.height || 0;
                        if (width && height && (width < 96 || height < 96)) continue;
                        push(img.currentSrc || img.src || largestFromSrcset(img.srcset), {width, height, kind: 'image'});
                    }
                    for (const source of Array.from(document.querySelectorAll('picture source[srcset]'))) {
                        push(largestFromSrcset(source.srcset), {kind: 'image'});
                    }
                    for (const el of Array.from(document.querySelectorAll('[style]'))) {
                        const bg = getComputedStyle(el).backgroundImage || '';
                        const match = bg.match(/url\\([\"']?([^\"')]+)[\"']?\\)/);
                        if (match) push(match[1], {kind: 'image'});
                    }
                    return out.filter(item => /^(blob:|data:image\\/|https?:\\/\\/)/i.test(item.url));
                }
                for (const video of Array.from(document.querySelectorAll('video'))) {
                    push(video.currentSrc || video.src, {
                        width: video.videoWidth || video.clientWidth || 0,
                        height: video.videoHeight || video.clientHeight || 0,
                        kind: 'video'
                    });
                    for (const source of Array.from(video.querySelectorAll('source'))) {
                        push(source.src, {kind: 'video'});
                    }
                }
                for (const source of Array.from(document.querySelectorAll('source[src]'))) {
                    push(source.src, {kind: 'video'});
                }
                for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    const href = a.href || '';
                    const text = (a.innerText || a.getAttribute('aria-label') || '').toLowerCase();
                    if (/\\.mp4|\\.webm|video|download|保存|下载/i.test(href) || /video|download|保存|下载/i.test(text)) {
                        push(href, {kind: 'video'});
                    }
                }
                return out.filter(item => /^(blob:|data:video\\/|https?:\\/\\/)/i.test(item.url) || /\\.mp4|\\.webm/i.test(item.url));
            }""",
            kind,
        ) or []

    @classmethod
    def _messages_to_prompt_and_media(cls, messages: list[ChatMessage]) -> tuple[str, list[str]]:
        parts: list[str] = []
        media: list[str] = []
        for message in messages:
            content, content_media = cls._content_to_text_and_media(message.content)
            media.extend(content_media)
            if content.strip():
                parts.append(f"{message.role}: {content.strip()}")
        return "\n".join(parts), media

    @classmethod
    def _content_to_text_and_media(cls, content: str | list[dict[str, Any]]) -> tuple[str, list[str]]:
        if isinstance(content, str):
            return content, []
        text_parts: list[str] = []
        media: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            media_value = cls._media_from_content_part(item)
            if media_value:
                media.append(media_value)
                continue
            text = item.get("text") or item.get("content")
            if text is not None:
                text_parts.append(str(text))
        return "\n".join(part for part in text_parts if part.strip()), media

    @staticmethod
    def _media_from_content_part(item: dict[str, Any]) -> str:
        for key in ("image_url", "input_image", "video_url", "input_video", "media_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("url") or value.get("image_url") or value.get("video_url")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        file_value = item.get("file")
        if isinstance(file_value, dict):
            for key in ("file_data", "data", "url"):
                value = file_value.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("file_data", "data"):
            value = item.get(key)
            if isinstance(value, str) and value.strip().startswith(("data:", "http://", "https://")):
                return value.strip()
        return ""

    @classmethod
    def _messages_to_prompt(cls, messages: list[ChatMessage]) -> str:
        prompt, _ = cls._messages_to_prompt_and_media(messages)
        return prompt


def b64_data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
