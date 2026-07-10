# Complete Flow

This document defines the complete grok2api operational flow.

## 1. Build Images

Build the API image:

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t grok2api:latest .
```

Build the account browser image:

```bash
docker build -f Dockerfile.browser -t grok2api-browser:latest .
```

The browser image runs Chrome DevTools on an internal loopback port and exposes container port `9222` through a TCP proxy. This is required for modern Chromium builds that do not publish CDP on `0.0.0.0` directly.

## 2. Run API Service

The API container must mount:

- `/app/data` for SQLite, profiles, generated media, and diagnostics
- `/var/run/docker.sock` so it can start account-scoped browser containers

Example:

```bash
docker run -d --name grok2api \
  --restart unless-stopped \
  --env-file /opt/ai-aggregator/data/grok2api/service.env \
  --add-host host.docker.internal:host-gateway \
  -p 18024:18024 \
  -v /opt/ai-aggregator/data/grok2api:/app/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  grok2api:latest
```

Account browser containers are started with a larger shared-memory segment and
`seccomp=unconfined` so Chromium's sandbox can create its namespaces. Chrome runs
as a non-root browser account (`pwuser`, or `chrome` as a fallback) with the
Chromium sandbox enabled. The browser entrypoint prefers the real browser binary
instead of package wrapper scripts and avoids `--no-sandbox` and unrelated
`--disable-*` switches; only the persistent profile path and local CDP endpoint
are configured explicitly.
The browser image includes Mesa/GLX and DBus packages so desktop browser APIs
have the required runtime dependencies. On CPU-only Xvfb hosts, Chrome may still
blocklist llvmpipe WebGL; use a real GPU desktop host when WebGL identity must
match a normal consumer Chrome session. Set `GROK2API_BROWSER_TIMEZONE` to the
account's normal region; HZ01 uses `Asia/Taipei`.
If `https://accounts.x.ai/check-login` returns `Blocked due to abusive traffic
patterns` even before login, use `GROK2API_BROWSER_PROXY_SERVER` to route only
the account browser through a clean HTTP or SOCKS egress. This changes the
browser's outbound network path without changing the API service URL.

## 3. Add Account

Open Admin:

```text
http://SERVER_IP:18024/admin
```

Enter the Admin key when prompted. Admin API calls use `Authorization: Bearer <admin-key>`; query-string keys remain accepted only for compatibility.

The service entrypoint defaults `GROK2API_ACCESS_LOG=false` to avoid logging compatibility query-string secrets.

Create an account. Cookie header is optional. If provided, it is only import material.

## 4. Start Remote Browser

Click `Open Login Browser`.

grok2api starts:

```text
grok2api-browser-<account_id>
```

The returned URL points to the account noVNC session:

```text
http://SERVER_IP:<account-port>/vnc.html
```

If a browser image has been rebuilt, use `Recreate Browser` for existing account containers so the new entrypoint and CDP proxy take effect.

## 5. Log In

Use the noVNC page to log in to:

```text
https://grok.com/
https://grok.com/imagine
```

This preserves the complete browser profile under:

```text
/opt/ai-aggregator/data/grok2api/profiles/<account_id>
```

## 6. Validate

Click `Validate`.

Current validation checks:

- Docker browser container is running
- Chrome DevTools endpoint is reachable, with multiple candidate URLs probed
- Grok Web login state through Playwright CDP
- Prompt input availability on `https://grok.com/`
- Basic Images page reachability when the prompt input is available
- account capabilities inferred from the reachable web surfaces

Cookie material is still stored as import material, but it is no longer required for a profile to be marked ready. If `GROK2API_COOKIE_WRITEBACK=true`, cookies observed from a successful browser session are written back to SQLite as import material.

## 7. API Contract

Current API contract:

- no ready account returns `409 provider_login_required`
- chat drives `https://grok.com/`
- chat streaming uses OpenAI-compatible SSE chunks after the browser response is available
- image generation drives `https://grok.com/imagine`
- image/video request fields are applied as prompt constraints and best-effort local/data-URL uploads
- video generation drives `https://grok.com/imagine` and waits for video sources
- generated image/video media is fetched in the browser context and returned as service-local `/v1/files/...` URLs when possible
- every chat/image/video request writes a SQLite task record
- `/v1/tasks/{task_id}` returns task status, sanitized request metadata, result metadata, and errors
- adapter failures write screenshot, HTML, and trace diagnostics under `/app/data/diagnostics`

This avoids false positives for text, image, or video generation: a browser must be running, CDP must be reachable, and the account must validate as ready.

## 8. Residual External Limits

The implementation is complete for the repository-owned control loop, but the upstream web surface is not owned by grok2api:

- Grok Web DOM selectors can change.
- Grok Imagine video can be unavailable because of account, quota, region, or rollout state.
- noVNC still needs deployment-level access control before public exposure.
