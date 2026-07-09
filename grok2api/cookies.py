from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any


def parse_cookie_header(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    parsed = SimpleCookie()
    try:
        parsed.load(text)
        return {key: morsel.value for key, morsel in parsed.items()}
    except Exception:
        result: dict[str, str] = {}
        for part in text.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                result[key] = value.strip()
        return result


def cookie_dict_to_playwright(cookies: dict[str, str]) -> list[dict[str, Any]]:
    domains = (".grok.com", ".x.com", ".twitter.com")
    return [
        {
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        }
        for domain in domains
        for name, value in cookies.items()
    ]


def required_grok_cookie_score(cookies: dict[str, str]) -> int:
    important = {
        "auth_token",
        "ct0",
        "twid",
        "guest_id",
        "guest_id_ads",
        "guest_id_marketing",
        "personalization_id",
        "cf_clearance",
        "grok_session",
        "grok_sessionid",
        "__Secure-next-auth.session-token",
    }
    return sum(1 for name in important if name in cookies)
