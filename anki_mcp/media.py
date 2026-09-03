"""Fetch card images from AnkiWeb on demand and shrink them for the model.

Uses the official media sync protocol's downloadFiles call, authenticated with the
same host key as the collection sync. Only the files a card references are
fetched, never the whole media folder, and results are cached on disk.
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import string
import threading
import zipfile

import httpx
import zstandard
from PIL import Image as PILImage

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://sync.ankiweb.net/"
SYNC_VERSION = 11
CLIENT_VERSION = "anki,26.08.1 (anki-mcp),python"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def is_image_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_SUFFIXES


class MediaStore:
    """Per-file media downloads with a disk cache of shrunken JPEGs."""

    def __init__(
        self,
        cache_dir: str,
        max_edge: int = 1280,
        quality: int = 80,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.cache_dir = cache_dir
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self.max_edge = max_edge
        self.quality = quality
        self.hkey: str | None = None
        self.endpoint = DEFAULT_ENDPOINT
        self.lock = threading.Lock()
        self.session_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))

    def set_auth(self, hkey: str, endpoint: str | None) -> None:
        self.hkey = hkey
        self.endpoint = endpoint or DEFAULT_ENDPOINT

    def get(self, names: list[str]) -> dict[str, bytes | None]:
        """JPEG bytes per file name, or None when a file cannot be fetched or decoded."""
        out: dict[str, bytes | None] = {}
        wanted = [n for n in dict.fromkeys(names) if is_image_file(n)]
        missing: list[str] = []
        for n in wanted:
            cached = self._read_cache(n)
            if cached is not None:
                out[n] = cached
            else:
                missing.append(n)
        if missing:
            try:
                raw = self._download(missing)
            except Exception:
                log.exception("media download failed for %s", missing)
                raw = {}
            for n in missing:
                data = raw.get(n)
                out[n] = self._shrink_and_cache(n, data) if data else None
        for n in names:
            out.setdefault(n, None)
        return out

    # --- internals ------------------------------------------------------------------------

    def _cache_path(self, name: str) -> str:
        safe = name.replace("/", "_").replace("\\", "_")
        return os.path.join(self.cache_dir, safe + ".jpg")

    def _read_cache(self, name: str) -> bytes | None:
        try:
            with open(self._cache_path(name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def _shrink_and_cache(self, name: str, data: bytes) -> bytes | None:
        try:
            im = PILImage.open(io.BytesIO(data))
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = PILImage.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((self.max_edge, self.max_edge))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=self.quality, optimize=True)
        except Exception:
            log.exception("could not decode image %s", name)
            return None
        jpeg = buf.getvalue()
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._cache_path(name), "wb") as f:
                f.write(jpeg)
        except OSError:
            log.warning("could not cache %s", name)
        return jpeg

    def _download(self, names: list[str]) -> dict[str, bytes]:
        if not self.hkey:
            raise RuntimeError("media store has no AnkiWeb host key yet")
        header = json.dumps({"v": SYNC_VERSION, "k": self.hkey, "c": CLIENT_VERSION, "s": self.session_id})
        body = zstandard.ZstdCompressor().compress(json.dumps({"files": names}).encode())
        with self.lock:
            r = self.http.post(
                self.endpoint + "msync/downloadFiles",
                content=body,
                headers={"anki-sync": header, "content-type": "application/octet-stream"},
            )
        r.raise_for_status()
        data = zstandard.ZstdDecompressor().decompressobj().decompress(r.content)
        z = zipfile.ZipFile(io.BytesIO(data))
        meta: dict[str, str] = json.loads(z.read("_meta"))
        out = {meta[entry]: z.read(entry) for entry in z.namelist() if entry != "_meta" and entry in meta}
        log.info("downloaded %d/%d media file(s) from AnkiWeb", len(out), len(names))
        return out
