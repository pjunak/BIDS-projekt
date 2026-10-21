"""Shared helper for downloading open-data files incrementally.

`Store.fetch()` downloads a URL to a stable path under data/raw/<source>/ and records it in
manifest.json (URL, ETag, Last-Modified, SHA-256, size, dates). Next time it sends a conditional
request, so an unchanged file costs one tiny 304 response. When a file does change, the previous
version is moved to _history/ (unless keep_history=False), so the ETL can be re-run on old data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import requests

from .http import PoliteClient
from .util import now_utc, today_local

log = logging.getLogger(__name__)

# Outcomes of Store.fetch() / Store.put()
NEW, CHANGED, UNCHANGED, NOT_MODIFIED = "new", "changed", "unchanged", "not-modified"


class DownloadError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Store:
    def __init__(self, data_dir: Path, source: str):
        self.root = data_dir / "raw" / source
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.entries: dict[str, dict] = {}
        if self.manifest_path.exists():
            self.entries = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def path(self, rel: str) -> Path:
        return self.root / rel

    def has(self, rel: str) -> bool:
        return rel in self.entries and self.path(rel).exists()

    def save(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _download(self, client, url, tmp: Path, *, params, headers, decode) -> requests.Response | None:
        """Stream url into tmp. Returns the response (body consumed), or None on 304."""
        tmp.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            resp = client.get(url, params=params, headers=headers, stream=True)
            try:
                if resp.status_code == 304:
                    return None
                if resp.status_code != 200:
                    raise DownloadError(f"{resp.url}: HTTP {resp.status_code}")
                with open(tmp, "wb") as fh:
                    if decode:
                        chunks = resp.iter_content(1 << 20)
                    else:  # keep the bytes exactly as sent (e.g. .gz served with Content-Encoding: gzip)
                        chunks = resp.raw.stream(1 << 20, decode_content=False)
                    for chunk in chunks:
                        fh.write(chunk)
                return resp
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                log.warning("%s: transfer interrupted (%s), retrying", url, exc)
                if attempt == 2:
                    raise DownloadError(f"{url}: transfer failed: {exc}") from exc
            finally:
                resp.close()
        raise AssertionError("unreachable")

    def download_temp(
        self, client: PoliteClient, url: str, *, params: dict | str | None = None
    ) -> tuple[Path, requests.structures.CaseInsensitiveDict]:
        """Download to a scratch file the caller processes and deletes (not tracked in the manifest).
        Returns the file and the response headers."""
        tmp = self.root / "_tmp" / (url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "download")
        resp = self._download(client, url, tmp, params=params, headers=None, decode=True)
        if resp is None:
            raise DownloadError(f"{url}: unexpected 304")
        return tmp, resp.headers

    def put(self, rel: str, tmp: Path, record: dict, *, keep_history: bool = True) -> str:
        """Move a finished file into place at rel, archiving the previous version if it differs."""
        dest = self.path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry = self.entries.get(rel) if dest.exists() else None
        sha = sha256_file(tmp)
        now = now_utc()
        if entry and entry.get("sha256") == sha:
            tmp.unlink()
            entry.update({k: v for k, v in record.items() if k in ("etag", "last_modified")}, checked_at=now)
            self.save()
            return UNCHANGED
        outcome = CHANGED if entry else NEW
        if entry and keep_history:
            hist = self.root / "_history" / Path(rel).parent / f"{dest.stem}__{today_local()}{dest.suffix}"
            hist.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dest, hist)
        tmp.replace(dest)
        size = dest.stat().st_size
        self.entries[rel] = {
            **record, "sha256": sha, "size": size, "downloaded_at": now, "checked_at": now, "changed_at": now,
        }
        self.save()
        log.info("%s %s (%.1f MB)", outcome, rel, size / 1e6)
        return outcome

    def fetch(
        self,
        client: PoliteClient,
        url: str,
        rel: str,
        *,
        params: dict | str | None = None,
        headers: dict | None = None,
        conditional: bool = True,
        keep_history: bool = True,
        decode: bool = True,
        extra: dict | None = None,
    ) -> str:
        """Download url to <root>/<rel>, using a conditional request if we have a previous copy."""
        dest = self.path(rel)
        entry = self.entries.get(rel) if dest.exists() else None
        req_headers = dict(headers or {})
        if conditional and entry:
            if entry.get("etag"):
                req_headers["If-None-Match"] = entry["etag"]
            if entry.get("last_modified"):
                req_headers["If-Modified-Since"] = entry["last_modified"]
        tmp = dest.with_name(dest.name + ".part")
        resp = self._download(client, url, tmp, params=params, headers=req_headers, decode=decode)
        if resp is None:
            if entry:
                entry["checked_at"] = now_utc()
                self.save()
                return NOT_MODIFIED
            raise DownloadError(f"{url}: 304 without a local copy")
        record = {
            "url": resp.url,
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            **(extra or {}),
        }
        return self.put(rel, tmp, record, keep_history=keep_history)
