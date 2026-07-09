# grok2api Architecture

`grok2api` is a Grok Web profile-browser reverse proxy.

The stable identity is not a raw Cookie header. The stable identity is a server-side Chromium profile that the user can operate through noVNC and that the API can automate through Chrome DevTools Protocol.

## Components

| Component | Responsibility |
|---|---|
| FastAPI server | Admin API, OpenAI-compatible API surface, authentication, task polling |
| SQLite store | Accounts, login sessions, task log, account capabilities, account metrics |
| Browser kernel | Account profile paths, browser lifecycle, CDP endpoint probing, Playwright orchestration, account locks |
| noVNC Chromium | User-operated server-side browser for login and verification |
| CDP proxy | Container-level TCP proxy from `0.0.0.0:9222` to Chrome's loopback DevTools port |
| Grok adapters | Browser CDP implementations for chat and account-available Grok Imagine image/video surfaces |
| Media store | Saves fetched image/video media under the service data directory and serves opaque `/v1/files/...` URLs |
| Diagnostics store | Saves screenshots, HTML, and Playwright traces for adapter failures |

## Account Lifecycle

1. Create an account in Admin.
2. Optionally paste Grok request Cookie header.
3. Create an interactive browser login session.
4. Open the returned noVNC URL.
5. Complete Grok/X login in the server-side Chromium.
6. Validate account state by opening:
   - `https://grok.com/`
   - `https://grok.com/imagine`
7. Validation records account capabilities and marks the account `ready` only when the prompt input is available.
8. API requests reuse the same account profile through the CDP adapter.

## Request Lifecycle

1. The API request is authenticated.
2. A task row is created with a sanitized request payload.
3. A ready account is selected, preferring accounts with fewer recent task failures.
4. The account lock is acquired.
5. The browser CDP endpoint is probed from multiple candidate URLs.
6. Playwright connects over CDP and drives the relevant Grok Web surface.
7. Media outputs are fetched in the browser context and saved locally when needed.
8. The task row is updated to `completed` or `failed`.
9. Failures capture screenshot, HTML, and trace diagnostics when the page is reachable.

## Why Not Cookie Only

Cookies do not contain the full browser state. Grok may depend on device state, local storage, IndexedDB, service workers, and risk checks. Raw Cookies are therefore only import material; the durable account state must be a Chromium profile.

## Media Strategy

Grok media must be driven through the logged-in Grok Web surface rather than cookie-only replay.

| Capability | Target Web Surface |
|---|---|
| Text | `https://grok.com/` |
| Image | `https://grok.com/imagine` |
| Video | `https://grok.com/imagine` when video generation is available to the account |

The adapter uses DOM extraction plus browser-context `fetch()` to convert protected/blob media into service-local downloadable files. Network-response extraction and download-button extraction can still be added later as additional strategies if xAI changes the DOM surface.
