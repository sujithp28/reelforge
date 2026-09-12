"""Object storage abstraction.

The database stores an opaque `storage key` (for example
`uploads/proj_abc/asset_def.jpg`), never an absolute filesystem path. That is
what lets local disk be swapped for S3-compatible storage without touching
any stored row or any frontend URL.

FFmpeg needs a real file on disk, so every backend must be able to produce a
local path for a key. `LocalStorage` returns the file in place; a remote
backend downloads it to a temporary file first. That is the whole contract.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from . import config


def upload_key(project_id: str, asset_id: str, suffix: str) -> str:
    return f"uploads/{project_id}/{asset_id}{suffix}"


def render_key(project_id: str) -> str:
    return f"renders/{project_id}.mp4"


def clip_key(project_id: str, scene_id: str, fingerprint: str) -> str:
    """Where one scene's generated clip is cached.

    The fingerprint is in the key, so changing a scene's inputs writes a new
    object instead of overwriting the old one mid-render.
    """
    return f"clips/{project_id}/{scene_id}_{fingerprint}.mp4"


def kaggle_staging_key(job_id: str) -> str:
    """Where an external worker's raw upload is staged before normalisation."""
    return f"kaggle/{job_id}.mp4"


class Storage(Protocol):
    """Minimal surface the app needs. Implement all five for a new backend."""

    def save_bytes(self, key: str, payload: bytes) -> str: ...
    def save_file(self, key: str, source: Path, move: bool = True) -> str: ...
    def localize(self, key: str) -> Path | None: ...
    def url_for(self, key: str | None) -> str | None: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorage:
    """Files under DATA_DIR, served by the app at MEDIA_URL_PREFIX."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # Keys are app-generated, but refuse traversal rather than trust that.
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError(f"storage key escapes the root: {key!r}")
        return target

    def save_bytes(self, key: str, payload: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return key

    def save_file(self, key: str, source: Path, move: bool = True) -> str:
        """Store a file. `move=False` leaves the source in place.

        A cached scene clip must be copied: the assembly step still needs the
        original where it is.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).resolve() != path:
            if move:
                shutil.move(str(source), path)
            else:
                shutil.copy2(str(source), path)
        return key

    def localize(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.exists() else None

    def url_for(self, key: str | None) -> str | None:
        if not key:
            return None
        return f"{config.MEDIA_URL_PREFIX}/{key.lstrip('/')}"

    def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)


class S3Storage:
    """S3-compatible backend. Integration boundary — not wired up yet.

    Deliberately unimplemented so the seam is explicit rather than implied.
    Completing it needs boto3 and roughly this shape:

        save_bytes  -> client.put_object(Bucket, Key=key, Body=payload)
        save_file   -> client.upload_file(str(source), Bucket, key)
        localize    -> client.download_file(Bucket, key, tmp) and return tmp
        url_for     -> f"{public_base}/{key}" or a presigned GET
        delete_prefix -> list_objects_v2 then delete_objects

    Nothing else in the app changes: keys stay the same, and `url_for` already
    returns absolute URLs correctly through the frontend's `mediaUrl`.
    """

    def __init__(self, bucket: str, endpoint: str = "", public_base: str = ""):
        self.bucket = bucket
        self.endpoint = endpoint
        self.public_base = public_base

    def _unimplemented(self) -> None:
        raise NotImplementedError(
            "S3Storage is an integration boundary and is not implemented yet."
            " Set REELFORGE_STORAGE=local, or implement S3Storage with boto3."
        )

    def save_bytes(self, key: str, payload: bytes) -> str:
        self._unimplemented()
        raise AssertionError("unreachable")

    def save_file(self, key: str, source: Path, move: bool = True) -> str:
        self._unimplemented()
        raise AssertionError("unreachable")

    def localize(self, key: str) -> Path | None:
        self._unimplemented()
        raise AssertionError("unreachable")

    def url_for(self, key: str | None) -> str | None:
        if not key:
            return None
        base = self.public_base or f"{self.endpoint}/{self.bucket}"
        return f"{base.rstrip('/')}/{key.lstrip('/')}"

    def delete_prefix(self, prefix: str) -> None:
        self._unimplemented()


def build_storage() -> Storage:
    if config.STORAGE_BACKEND == "s3":
        if not config.S3_BUCKET:
            raise RuntimeError("REELFORGE_STORAGE=s3 requires REELFORGE_S3_BUCKET")
        return S3Storage(config.S3_BUCKET, config.S3_ENDPOINT, config.S3_PUBLIC_BASE)
    if config.STORAGE_BACKEND != "local":
        raise RuntimeError(f"unknown REELFORGE_STORAGE={config.STORAGE_BACKEND!r}")
    return LocalStorage(config.DATA_DIR)


storage: Storage = build_storage()
