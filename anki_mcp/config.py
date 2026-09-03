"""Configuration from the environment. Secrets are read here and nowhere else."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    ankiweb_username: str  # secret
    ankiweb_password: str  # secret
    owner_password: str  # secret: typed on the OAuth consent page
    jwt_signing_key: str  # secret: signs auth codes and tokens
    base_url: str  # public URL of this server, no trailing slash
    port: int = 8080
    collection_dir: str = "/tmp/anki-mcp"
    extra_redirect_uris: list[str] = field(default_factory=list)
    idle_sync_seconds: int = 90  # flush an unsynced grade after this much inactivity
    media_cache_dir: str = "/tmp/anki-mcp/media-cache"  # shrunken card images fetched from AnkiWeb
    image_max_edge: int = 1280  # images are downscaled to this many pixels on the long side

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        required = ["ANKIWEB_USERNAME", "ANKIWEB_PASSWORD", "OWNER_PASSWORD", "JWT_SIGNING_KEY", "BASE_URL"]
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
        if len(e["JWT_SIGNING_KEY"]) < 32:
            raise RuntimeError(
                "JWT_SIGNING_KEY must be at least 32 characters (try: openssl rand -base64 48)"
            )
        extra = [u.strip() for u in e.get("EXTRA_REDIRECT_URIS", "").split(",") if u.strip()]
        collection_dir = e.get("COLLECTION_DIR", "/tmp/anki-mcp")
        return cls(
            ankiweb_username=e["ANKIWEB_USERNAME"],
            ankiweb_password=e["ANKIWEB_PASSWORD"],
            owner_password=e["OWNER_PASSWORD"],
            jwt_signing_key=e["JWT_SIGNING_KEY"],
            base_url=e["BASE_URL"].rstrip("/"),
            port=int(e.get("PORT", "8080")),
            collection_dir=collection_dir,
            extra_redirect_uris=extra,
            idle_sync_seconds=int(e.get("IDLE_SYNC_SECONDS", "90")),
            media_cache_dir=e.get("MEDIA_CACHE_DIR", os.path.join(collection_dir, "media-cache")),
            image_max_edge=int(e.get("IMAGE_MAX_EDGE", "1280")),
        )
