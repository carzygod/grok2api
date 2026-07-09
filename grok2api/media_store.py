from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path

from .config import settings


MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


class MediaStore:
    def __init__(self, root: Path | None = None):
        self.root = root or settings.downloads_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def save_b64(self, data: str, *, media_type: str = "image/png", prefix: str = "image") -> dict:
        raw = self._decode_b64(data)
        ext = MEDIA_TYPES.get((media_type or "").lower(), ".bin")
        digest = hashlib.sha256(raw).hexdigest()[:24]
        file_id = f"{prefix}-{int(time.time())}-{digest}{ext}"
        path = self._safe_path(file_id)
        path.write_bytes(raw)
        return {
            "id": file_id,
            "path": str(path),
            "media_type": media_type or "application/octet-stream",
            "size": len(raw),
            "url_path": f"/v1/files/{file_id}",
        }

    def path_for(self, file_id: str) -> Path:
        return self._safe_path(file_id)

    def _safe_path(self, file_id: str) -> Path:
        clean = "".join(ch for ch in file_id if ch.isalnum() or ch in {".", "-", "_"})
        if clean != file_id or not clean:
            raise ValueError("invalid_file_id")
        path = (self.root / clean).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError("invalid_file_path")
        return path

    @staticmethod
    def _decode_b64(data: str) -> bytes:
        value = (data or "").strip()
        if "," in value and value.lower().startswith("data:"):
            value = value.split(",", 1)[1]
        return base64.b64decode(value, validate=False)
