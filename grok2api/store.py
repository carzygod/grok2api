from __future__ import annotations

import json
import secrets
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import settings
from .cookies import parse_cookie_header
from .models import Account, TaskRecord


def _now() -> int:
    return int(time.time())


def _next_daily_reset(now: int | None = None) -> int:
    current = datetime.fromtimestamp(now or _now())
    reset = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(reset.timestamp())


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_account_quotas (
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    window TEXT NOT NULL DEFAULT 'day',
                    limit_units INTEGER NOT NULL,
                    used_units INTEGER NOT NULL DEFAULT 0,
                    reserved_units INTEGER NOT NULL DEFAULT 0,
                    reset_at INTEGER NOT NULL,
                    cooldown_until INTEGER,
                    cooldown_reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (account_id, kind, window)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_account_quotas_kind ON grok_account_quotas(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_account_quotas_reset ON grok_account_quotas(reset_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_usage_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_usage_events_task ON grok_usage_events(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_usage_events_account ON grok_usage_events(account_id)")

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

    def _default_quota_limit(self, kind: str) -> int:
        if kind == "video":
            return max(0, int(settings.video_daily_quota))
        return max(0, int(settings.image_daily_quota))

    def _quota_row(self, conn: sqlite3.Connection, account_id: str, kind: str, *, now: int | None = None) -> sqlite3.Row:
        current = now or _now()
        row = conn.execute(
            """
            SELECT * FROM grok_account_quotas
            WHERE account_id = ? AND kind = ? AND window = 'day'
            """,
            (account_id, kind),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO grok_account_quotas (
                    account_id, kind, window, limit_units, used_units, reserved_units,
                    reset_at, cooldown_until, cooldown_reason, updated_at
                ) VALUES (?, ?, 'day', ?, 0, 0, ?, NULL, '', ?)
                """,
                (account_id, kind, self._default_quota_limit(kind), _next_daily_reset(current), current),
            )
        elif int(row["reset_at"] or 0) <= current:
            conn.execute(
                """
                UPDATE grok_account_quotas
                SET used_units = 0,
                    reserved_units = 0,
                    reset_at = ?,
                    updated_at = ?
                WHERE account_id = ? AND kind = ? AND window = 'day'
                """,
                (_next_daily_reset(current), current, account_id, kind),
            )
        row = conn.execute(
            """
            SELECT * FROM grok_account_quotas
            WHERE account_id = ? AND kind = ? AND window = 'day'
            """,
            (account_id, kind),
        ).fetchone()
        if row is None:  # pragma: no cover - insert/select above should guarantee a row.
            raise RuntimeError("quota_row_missing")
        return row

    def quota_status(self, account_id: str, kind: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = self._quota_row(conn, account_id, kind)
            return self._row_to_quota(row)

    def list_generation_quotas(self, account_id: str | None = None) -> list[dict[str, Any]]:
        accounts = [self.get(account_id)] if account_id else self.list_accounts()
        result: list[dict[str, Any]] = []
        for account in accounts:
            if account is None:
                continue
            for kind in ("image", "video"):
                quota = self.quota_status(account.id, kind)
                quota["account_name"] = account.name
                quota["account_status"] = account.status
                result.append(quota)
        return result

    def reserve_generation_quota(self, account_id: str, kind: str, units: int, task_id: str) -> dict[str, Any]:
        units = max(1, int(units or 1))
        now = _now()
        with self._lock, self._connect() as conn:
            row = self._quota_row(conn, account_id, kind, now=now)
            quota = self._row_to_quota(row)
            cooldown_until = int(quota.get("cooldown_until") or 0)
            if cooldown_until > now:
                return {
                    "ok": False,
                    "reason": "cooldown",
                    "retry_after": cooldown_until - now,
                    "quota": quota,
                }
            if quota["limit_units"] >= 0 and units > quota["remaining_units"]:
                return {
                    "ok": False,
                    "reason": "quota_exhausted",
                    "retry_after": max(0, int(quota["reset_at"]) - now),
                    "quota": quota,
                }
            reservation_id = "quota-" + uuid.uuid4().hex
            conn.execute(
                """
                UPDATE grok_account_quotas
                SET reserved_units = reserved_units + ?,
                    updated_at = ?
                WHERE account_id = ? AND kind = ? AND window = 'day'
                """,
                (units, now, account_id, kind),
            )
            conn.execute(
                """
                INSERT INTO grok_usage_events (
                    id, task_id, account_id, kind, units, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', '', ?)
                """,
                (reservation_id, task_id, account_id, kind, units, now),
            )
            row = self._quota_row(conn, account_id, kind, now=now)
            return {
                "ok": True,
                "reservation_id": reservation_id,
                "task_id": task_id,
                "account_id": account_id,
                "kind": kind,
                "units": units,
                "quota": self._row_to_quota(row),
            }

    def commit_generation_quota(self, reservation: dict[str, Any], reason: str = "completed") -> dict[str, Any]:
        return self._finish_generation_quota(reservation, status="committed", reason=reason)

    def release_generation_quota(self, reservation: dict[str, Any], reason: str = "released") -> dict[str, Any]:
        return self._finish_generation_quota(reservation, status="released", reason=reason)

    def _finish_generation_quota(self, reservation: dict[str, Any], *, status: str, reason: str) -> dict[str, Any]:
        account_id = str(reservation.get("account_id") or "")
        kind = str(reservation.get("kind") or "")
        task_id = str(reservation.get("task_id") or "")
        units = max(1, int(reservation.get("units") or 1))
        if not account_id or not kind:
            return {}
        now = _now()
        with self._lock, self._connect() as conn:
            if status == "committed":
                conn.execute(
                    """
                    UPDATE grok_account_quotas
                    SET used_units = used_units + ?,
                        reserved_units = MAX(0, reserved_units - ?),
                        updated_at = ?
                    WHERE account_id = ? AND kind = ? AND window = 'day'
                    """,
                    (units, units, now, account_id, kind),
                )
            else:
                conn.execute(
                    """
                    UPDATE grok_account_quotas
                    SET reserved_units = MAX(0, reserved_units - ?),
                        updated_at = ?
                    WHERE account_id = ? AND kind = ? AND window = 'day'
                    """,
                    (units, now, account_id, kind),
                )
            conn.execute(
                """
                INSERT INTO grok_usage_events (
                    id, task_id, account_id, kind, units, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("quota-" + uuid.uuid4().hex, task_id, account_id, kind, units, status, reason, now),
            )
            row = self._quota_row(conn, account_id, kind, now=now)
            return self._row_to_quota(row)

    def mark_generation_cooldown(
        self,
        account_id: str,
        kind: str,
        *,
        reason: str,
        seconds: int,
    ) -> dict[str, Any]:
        now = _now()
        until = now + max(60, int(seconds or 60))
        with self._lock, self._connect() as conn:
            self._quota_row(conn, account_id, kind, now=now)
            conn.execute(
                """
                UPDATE grok_account_quotas
                SET cooldown_until = ?,
                    cooldown_reason = ?,
                    updated_at = ?
                WHERE account_id = ? AND kind = ? AND window = 'day'
                """,
                (until, reason[:240], now, account_id, kind),
            )
            row = self._quota_row(conn, account_id, kind, now=now)
            return self._row_to_quota(row)

    def update_generation_quota(
        self,
        account_id: str,
        kind: str,
        *,
        limit_units: int | None = None,
        used_units: int | None = None,
        reserved_units: int | None = None,
        cooldown_until: int | None = None,
        cooldown_reason: str | None = None,
        reset_used: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            self._quota_row(conn, account_id, kind, now=now)
            assignments: list[str] = []
            values: list[Any] = []
            if limit_units is not None:
                assignments.append("limit_units = ?")
                values.append(max(0, int(limit_units)))
            if used_units is not None:
                assignments.append("used_units = ?")
                values.append(max(0, int(used_units)))
            if reserved_units is not None:
                assignments.append("reserved_units = ?")
                values.append(max(0, int(reserved_units)))
            if reset_used:
                assignments.append("used_units = 0")
                assignments.append("reserved_units = 0")
                assignments.append("reset_at = ?")
                values.append(_next_daily_reset(now))
            if cooldown_until is not None:
                assignments.append("cooldown_until = ?")
                values.append(max(0, int(cooldown_until)) or None)
            if cooldown_reason is not None:
                assignments.append("cooldown_reason = ?")
                values.append(cooldown_reason[:240])
            if assignments:
                assignments.append("updated_at = ?")
                values.append(now)
                values.extend([account_id, kind])
                conn.execute(
                    f"""
                    UPDATE grok_account_quotas
                    SET {', '.join(assignments)}
                    WHERE account_id = ? AND kind = ? AND window = 'day'
                    """,
                    values,
                )
            row = self._quota_row(conn, account_id, kind, now=now)
            return self._row_to_quota(row)

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

    def _row_to_quota(self, row: sqlite3.Row) -> dict[str, Any]:
        limit_units = int(row["limit_units"])
        used_units = int(row["used_units"])
        reserved_units = int(row["reserved_units"])
        remaining_units = max(0, limit_units - used_units - reserved_units)
        cooldown_until = row["cooldown_until"]
        now = _now()
        return {
            "account_id": row["account_id"],
            "kind": row["kind"],
            "window": row["window"],
            "limit_units": limit_units,
            "used_units": used_units,
            "reserved_units": reserved_units,
            "remaining_units": remaining_units,
            "reset_at": row["reset_at"],
            "cooldown_until": cooldown_until,
            "cooldown_reason": row["cooldown_reason"] or "",
            "cooldown_active": bool(cooldown_until and int(cooldown_until) > now),
            "updated_at": row["updated_at"],
        }

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
