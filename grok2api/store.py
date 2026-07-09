from __future__ import annotations

import json
import secrets
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings
from .cookies import parse_cookie_header
from .models import Account, TaskRecord


def _now() -> int:
    return int(time.time())


def _safe_id(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-_")
    return value or f"acct-{uuid.uuid4().hex[:10]}"


def _account_port(account_id: str, base: int) -> int:
    checksum = sum(ord(ch) for ch in account_id)
    return base + (checksum % 700)


class AccountStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'new',
                    user_data_dir TEXT NOT NULL,
                    browser_container TEXT NOT NULL DEFAULT '',
                    browser_port INTEGER,
                    browser_debug_port INTEGER,
                    browser_password TEXT NOT NULL DEFAULT '',
                    capability_json TEXT NOT NULL DEFAULT '[]',
                    cookie_json TEXT NOT NULL DEFAULT '{}',
                    last_validated_at INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(grok_accounts)").fetchall()}
            migrations = {
                "browser_container": "ALTER TABLE grok_accounts ADD COLUMN browser_container TEXT NOT NULL DEFAULT ''",
                "browser_port": "ALTER TABLE grok_accounts ADD COLUMN browser_port INTEGER",
                "browser_debug_port": "ALTER TABLE grok_accounts ADD COLUMN browser_debug_port INTEGER",
                "browser_password": "ALTER TABLE grok_accounts ADD COLUMN browser_password TEXT NOT NULL DEFAULT ''",
                "capability_json": "ALTER TABLE grok_accounts ADD COLUMN capability_json TEXT NOT NULL DEFAULT '[]'",
            }
            for column, sql in migrations.items():
                if column not in cols:
                    conn.execute(sql)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_accounts_status ON grok_accounts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_accounts_enabled ON grok_accounts(enabled)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_login_sessions (
                    token TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    browser_url TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    account_id TEXT,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_tasks_account ON grok_tasks(account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_tasks_kind ON grok_tasks(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_tasks_status ON grok_tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_tasks_created ON grok_tasks(created_at)")

    def create_account(self, name: str = "", account_id: str = "", cookie_header: str = "") -> Account:
        raw_id = _safe_id(account_id or name)
        account_id = raw_id
        while self.get(account_id):
            account_id = f"{raw_id}-{uuid.uuid4().hex[:6]}"
        now = _now()
        user_data_dir = str(settings.profiles_dir / account_id)
        cookies = parse_cookie_header(cookie_header)
        status = "new" if not cookies else "login_required"
        browser_container = f"grok2api-browser-{account_id}"
        browser_port = _account_port(account_id, settings.browser_port_base)
        browser_debug_port = _account_port(account_id, settings.browser_debug_port_base)
        browser_password = "vnc-" + secrets.token_urlsafe(12)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO grok_accounts (
                    id, name, enabled, status, user_data_dir,
                    browser_container, browser_port, browser_debug_port, browser_password,
                    cookie_json,
                    created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    name or f"Grok account {account_id[-6:]}",
                    status,
                    user_data_dir,
                    browser_container,
                    browser_port,
                    browser_debug_port,
                    browser_password,
                    json.dumps(cookies, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(account_id)  # type: ignore[return-value]

    def list_accounts(self) -> list[Account]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM grok_accounts ORDER BY created_at ASC").fetchall()
        return [self._row_to_account(row) for row in rows]

    def get(self, account_id: str) -> Account | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM grok_accounts WHERE id = ?", (account_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def update_account(self, account_id: str, **fields: Any) -> Account | None:
        allowed = {
            "name",
            "enabled",
            "status",
            "cookie_header",
            "cookie_json",
            "browser_container",
            "browser_port",
            "browser_debug_port",
            "browser_password",
            "capabilities",
            "capability_json",
            "last_validated_at",
            "last_error",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "enabled":
                value = 1 if value else 0
            if key == "capabilities":
                key = "capability_json"
                value = json.dumps(list(value or []), ensure_ascii=False)
            elif key == "cookie_header":
                key = "cookie_json"
                value = json.dumps(parse_cookie_header(str(value)), ensure_ascii=False)
            elif key == "capability_json" and not isinstance(value, str):
                value = json.dumps(value or [], ensure_ascii=False)
            elif key == "cookie_json" and isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError:
                    value = json.dumps(parse_cookie_header(value), ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return self.get(account_id)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(account_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE grok_accounts SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get(account_id)

    def create_task(
        self,
        *,
        kind: str,
        model: str,
        prompt: str,
        account_id: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> TaskRecord:
        task_id = f"task-{kind}-{uuid.uuid4().hex}"
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO grok_tasks (
                    task_id, kind, model, account_id, status, prompt,
                    request_json, result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, '{}', '', ?, ?)
                """,
                (
                    task_id,
                    kind,
                    model,
                    account_id,
                    prompt,
                    json.dumps(request or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        task = self.get_task(task_id)
        if task is None:  # pragma: no cover - SQLite insert succeeded above.
            raise RuntimeError("task_insert_failed")
        return task

    def update_task(self, task_id: str, **fields: Any) -> TaskRecord | None:
        allowed = {"account_id", "status", "result", "result_json", "error"}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "result":
                key = "result_json"
                value = json.dumps(value or {}, ensure_ascii=False)
            elif key == "result_json" and not isinstance(value, str):
                value = json.dumps(value or {}, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return self.get_task(task_id)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(task_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE grok_tasks SET {', '.join(assignments)} WHERE task_id = ?", values)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM grok_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        limit: int = 50,
        account_id: str | None = None,
        kind: str | None = None,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            values.append(account_id)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit or 50), 500)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM grok_tasks {where} ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def account_metrics(self, *, since: int | None = None) -> dict[str, dict[str, Any]]:
        clauses = ["account_id IS NOT NULL", "account_id != ''"]
        values: list[Any] = []
        if since is not None:
            clauses.append("created_at >= ?")
            values.append(since)
        where = " AND ".join(clauses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT account_id, kind, status, COUNT(*) AS count, MAX(updated_at) AS last_seen
                FROM grok_tasks
                WHERE {where}
                GROUP BY account_id, kind, status
                """,
                values,
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            account_id = row["account_id"]
            account = result.setdefault(
                account_id,
                {
                    "account_id": account_id,
                    "total": 0,
                    "by_kind": {},
                    "by_status": {},
                    "last_seen": None,
                },
            )
            count = int(row["count"])
            account["total"] += count
            account["by_status"][row["status"]] = account["by_status"].get(row["status"], 0) + count
            kind_data = account["by_kind"].setdefault(row["kind"], {})
            kind_data[row["status"]] = kind_data.get(row["status"], 0) + count
            last_seen = row["last_seen"]
            if last_seen and (account["last_seen"] is None or last_seen > account["last_seen"]):
                account["last_seen"] = last_seen
        return result

    def create_login_session(self, account_id: str, token: str, browser_url: str, ttl: int) -> dict[str, Any]:
        now = _now()
        expires_at = now + max(60, ttl)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO grok_login_sessions (
                    token, account_id, browser_url, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token, account_id, browser_url, now, expires_at),
            )
        return {
            "account_id": account_id,
            "token": token,
            "browser_url": browser_url,
            "expires_at": expires_at,
        }

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        cookies = json.loads(row["cookie_json"] or "{}")
        try:
            capabilities = json.loads(row["capability_json"] or "[]")
        except (KeyError, json.JSONDecodeError):
            capabilities = []
        return Account(
            id=row["id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            status=row["status"],
            user_data_dir=row["user_data_dir"],
            browser_container=row["browser_container"] or "",
            browser_port=row["browser_port"],
            browser_debug_port=row["browser_debug_port"],
            browser_password=row["browser_password"] or "",
            cookie_count=len(cookies) if isinstance(cookies, dict) else 0,
            capabilities=capabilities if isinstance(capabilities, list) else [],
            last_validated_at=row["last_validated_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        def decode(raw: str, fallback: Any) -> Any:
            try:
                return json.loads(raw or "")
            except json.JSONDecodeError:
                return fallback

        return TaskRecord(
            task_id=row["task_id"],
            kind=row["kind"],
            model=row["model"],
            account_id=row["account_id"],
            status=row["status"],
            prompt=row["prompt"],
            request=decode(row["request_json"], {}),
            result=decode(row["result_json"], {}),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def account_cookies(self, account_id: str) -> dict[str, str]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT cookie_json FROM grok_accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["cookie_json"] or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
