# grok2api

`grok2api` is a Grok Web reverse proxy for account pools that need durable browser login state.

The project is intentionally built around persistent server-side Chromium profiles instead of raw Cookies. Cookies can help import an account, but the durable credential is the Chromium profile that a user can operate through noVNC.

## Repository Path

```text
C:\dev\sandbox\2api\grok2api
```

## HZ01 Deployment

The current HZ01 deployment is:

```text
http://192.168.31.26:18024
```

Remote layout:

```text
/opt/ai-aggregator/apps/grok2api
/opt/ai-aggregator/data/grok2api
```

Runtime secrets are intentionally not committed. Keep `GROK2API_ADMIN_KEY` and
`GROK2API_API_KEY` in the remote `service.env` file or another deployment secret
store. Local operator notes can live outside this repository, for example under
the workspace `.codex/` directory.

## Current Status

The browser-profile control loop is wired into the API surface:

- FastAPI service
- SQLite account store
- account-scoped Chromium profile paths
- raw Grok request Cookie header parsing
- interactive browser login session API
- Admin page for account creation, browser launch, validation, task inspection, and metrics
- OpenAI-compatible endpoint surface
- Dockerfile and noVNC Chromium browser image
- Playwright CDP adapter that attaches to the account browser and drives Grok pages
- local media cache for generated image/video files returned from browser-only URLs/blobs
- SQLite task log and `/v1/tasks/{task_id}` polling endpoint
- account capability tracking and task-based rotation metrics
- screenshot, HTML, and Playwright trace capture around adapter failures
- chat streaming response support using OpenAI-compatible SSE chunks

The closed loop is:

1. Create an account in Admin.
2. Open the account noVNC browser.
3. Log in to Grok inside that browser.
4. Run Validate.
5. Validate attaches to Chrome DevTools, opens Grok Web, checks login state, and marks the account `ready` only when the prompt input is reachable.
6. `/v1/chat/completions`, `/v1/images/generations`, and `/v1/video/generations` pick a ready account and drive the same browser profile.
7. Every generation request creates a task record and updates task/account state.
8. Image and video outputs are fetched in the browser context, saved under the service data directory, and returned as service-local `/v1/files/...` URLs unless image requests use `response_format=b64_json`.

The adapters still drive Grok Web, so they remain subject to Grok Web UI rollouts, account eligibility, regional availability, and risk checks. The service captures diagnostics and task state so those failures are observable without SSH-only inspection.

## Why This Exists

Plain cookie replay is brittle for Grok Web, especially when media generation or account risk checks are involved. This service drives the actual Grok Web surfaces:

| Capability | Target |
|---|---|
| Text | `https://grok.com/` |
| Image | `https://grok.com/imagine` |
| Video | `https://grok.com/imagine` when video generation is available to the account |

## Login Design

Raw Cookies are not enough. They do not fully cover:

- HttpOnly cookies that `document.cookie` cannot read
- localStorage
- IndexedDB
- service workers
- identity and risk checks
- device and browser profile state
- human verification state

The recommended flow is:

1. Create a Grok account in grok2api Admin.
2. Optionally paste a full Network Request `Cookie` header.
3. Click `Open Login Browser`.
4. grok2api starts one account-scoped noVNC Chromium container.
5. Open the returned browser URL.
6. Log in to Grok/X directly inside the server-side Chromium.
7. Keep that account's Chromium profile as the durable credential.
8. Run `Validate` to confirm the remote browser is reachable and ready for a real Grok canary.

## API Surface

Admin:

```http
GET /admin
GET /admin/api/accounts
POST /admin/api/accounts
PATCH /admin/api/accounts/{account_id}
POST /admin/api/accounts/{account_id}/login-session
POST /admin/api/accounts/{account_id}/validate
GET /admin/api/accounts/{account_id}/browser/status
POST /admin/api/accounts/{account_id}/browser/start
POST /admin/api/accounts/{account_id}/browser/stop
POST /admin/api/accounts/{account_id}/browser/recreate
GET /admin/api/browser/image-status
GET /admin/api/capabilities
GET /admin/api/tasks
GET /admin/api/tasks/{task_id}
GET /admin/api/metrics
```

Admin API calls should use `Authorization: Bearer <admin-key>`. Query-string `key` remains accepted for compatibility, but the bundled Admin page stores the key in session storage and sends headers to avoid repeating secrets in access logs.

The production entrypoint defaults `GROK2API_ACCESS_LOG=false` so URL secrets from compatibility clients are not written to Uvicorn access logs.

OpenAI-compatible:

```http
GET /v1/models
GET /v1/files/{file_id}
GET /v1/tasks/{task_id}
POST /v1/chat/completions
POST /v1/images/generations
POST /v1/video/generations
POST /v1/videos/generations
```

Request bodies support optional `account_id` so a caller can force a specific Grok browser profile.

## Build And Run

Do not run this locally for production login work. Build and run on the target server.

```bash
docker compose -f docker-compose.example.yml up -d --build
```

Manual Docker deployment:

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t grok2api:latest .
docker build -f Dockerfile.browser -t grok2api-browser:latest .

docker run -d --name grok2api \
  --restart unless-stopped \
  --env-file /opt/ai-aggregator/data/grok2api/service.env \
  --add-host host.docker.internal:host-gateway \
  -p 18024:18024 \
  -v /opt/ai-aggregator/data/grok2api:/app/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  grok2api:latest
```

Health checks:

```bash
curl http://SERVER_IP:18024/health
curl -H "Authorization: Bearer $GROK2API_API_KEY" http://SERVER_IP:18024/v1/models
curl -H "Authorization: Bearer $GROK2API_ADMIN_KEY" http://SERVER_IP:18024/admin/api/capabilities
```

## noVNC Chromium Kernel

Build the built-in browser image:

```bash
docker build -f Dockerfile.browser -t grok2api-browser:latest .
```

The browser image exposes:

| Port | Purpose |
|---|---|
| `5800` | noVNC web UI |
| `9222` | Chrome DevTools Protocol through a container TCP proxy |

Modern Chromium may bind DevTools to loopback even when asked to bind `0.0.0.0`. The browser image therefore runs Chrome DevTools on an internal loopback port and publishes a `socat` proxy on container port `9222` so Docker port publishing works.

Chrome is started as a non-root browser account (`pwuser`, or `chrome` when the
base image does not provide `pwuser`) with the Chromium sandbox enabled. The
entrypoint prefers the real browser binary, such as `/opt/google/chrome/chrome`
or `/usr/lib/chromium/chromium`, before falling back to package wrapper scripts.
The browser entrypoint intentionally avoids container/automation hardening flags such as
`--no-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--disable-breakpad`,
`--no-first-run`, `--no-default-browser-check`, `--password-store`, and
`--use-mock-keychain`. The only launch switches kept are the persistent profile directory
and the local Chrome DevTools endpoint required for account automation.
Browser containers are created with `--shm-size=1g` and
`--security-opt seccomp=unconfined` so the sandbox can create the namespaces it
expects without disabling the sandbox in Chrome.

Manual run example:

```bash
docker run -d --name grok2api-browser-default \
  --restart unless-stopped \
  --shm-size=1g \
  --security-opt seccomp=unconfined \
  -p 18200:5800 \
  -p 19200:9222 \
  -e DISPLAY_WIDTH=1440 \
  -e DISPLAY_HEIGHT=900 \
  -e VNC_PASSWORD='change-me-vnc-password' \
  -e START_URL='https://grok.com/' \
  -v ./data/profiles/default:/config \
  grok2api-browser:latest
```

Then open:

```text
http://SERVER_IP:18200/vnc.html
```

For production, do not expose noVNC directly to the public internet. Use private network access, a reverse proxy, or another deployment-level auth gateway.

## Data Layout

```text
data/
  grok2api.sqlite3
  profiles/
    account-a/
    account-b/
  downloads/
  diagnostics/
```

## Implemented Hardening

- Adapter failures save screenshot, page HTML, and Playwright trace artifacts under `data/diagnostics/`.
- Chat/image/video calls create SQLite task records and expose `/v1/tasks/{task_id}` plus Admin task views.
- Chat streaming is supported through OpenAI-compatible SSE chunks after the browser response is available.
- Account validation records detected capabilities, and account rotation considers recent task failures.
- Browser containers expose CDP through an internal loopback Chrome port plus a container-level TCP proxy.
- Request fields such as image count, size, video duration, aspect ratio, and reference media are applied as browser prompt constraints and best-effort uploads.

## Residual External Limits

- Grok Web selectors and media extraction can still break when xAI changes the UI.
- Grok Imagine video availability remains account, region, rollout, and quota dependent.
- noVNC URLs still need private-network access, a reverse proxy, or another deployment-level auth gateway before public exposure.

## Security Rules

- Never log full Cookie headers.
- Never commit `data/`, profile directories, SQLite databases, diagnostics, screenshots, traces, or generated media.
- noVNC URLs must be protected at the deployment layer.
- Manual login sessions should be short-lived.
- API requests lock an account profile while it is being automated.

## Current Capability Contract

| Endpoint | Current behavior |
|---|---|
| `/v1/models` | Returns the exposed Grok Web model list |
| `/v1/files/{file_id}` | Publicly serves generated local media files by opaque file id |
| `/v1/tasks/{task_id}` | Returns stored task status, sanitized request data, result metadata, and error details |
| `/v1/chat/completions` | Requires a ready account, drives `grok.com/` through CDP, and supports non-streaming or SSE streaming responses |
| `/v1/images/generations` | Requires a ready account, drives `grok.com/imagine`, applies count/size/reference constraints, and returns local downloadable URLs by default |
| `/v1/video/generations` | Requires a ready account, applies video constraints, waits for video sources on the Grok Web page, and returns local downloadable URLs when the media can be fetched |

This remains strict: a reachable Admin UI or a running Chromium profile is not enough. The account must pass browser validation before API requests can run.
