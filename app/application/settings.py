"""Typed application settings. Secrets (API hash, proxy secret) live only in the SecretStore."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..domain.models import ProxySettings
from ..domain.redact import register_secret
from ..infrastructure.build_config import bundled_api_credentials
from ..infrastructure.repositories import SettingsRepo
from .ports import SecretStore
from .scanner import DEFAULT_IGNORE


@dataclass
class AppSettings:
    speed_bytes_per_second: float | None = None  # None = unlimited
    concurrency: int = 4
    notifications: bool = True
    theme: str = "system"  # system | light | dark
    close_to_tray: bool = True
    ignore_patterns: tuple[str, ...] = field(default_factory=lambda: DEFAULT_IGNORE)
    preview_max_mb: int = 150


class SettingsService:
    def __init__(self, repo: SettingsRepo, secrets: SecretStore):
        self.repo = repo
        self.secrets = secrets

    # -- general -----------------------------------------------------------------------------
    def load(self) -> AppSettings:
        d = AppSettings()
        r = self.repo
        speed = r.get_json("speed_bps", "unset")
        d.speed_bytes_per_second = None if speed in ("unset", None) else float(speed)
        d.concurrency = max(1, min(8, int(r.get_json("concurrency", d.concurrency))))
        d.notifications = bool(r.get_json("notifications", d.notifications))
        d.theme = str(r.get_json("theme", d.theme))
        d.close_to_tray = bool(r.get_json("close_to_tray", d.close_to_tray))
        d.ignore_patterns = tuple(r.get_json("ignore_patterns", list(d.ignore_patterns)))
        d.preview_max_mb = int(r.get_json("preview_max_mb", d.preview_max_mb))
        return d

    def save(self, s: AppSettings) -> None:
        r = self.repo
        r.set_json("speed_bps", s.speed_bytes_per_second)
        r.set_json("concurrency", max(1, min(8, s.concurrency)))
        r.set_json("notifications", s.notifications)
        r.set_json("theme", s.theme)
        r.set_json("close_to_tray", s.close_to_tray)
        r.set_json("ignore_patterns", list(s.ignore_patterns))
        r.set_json("preview_max_mb", s.preview_max_mb)

    # -- proxy ---------------------------------------------------------------------------------
    def get_proxy(self) -> ProxySettings:
        secret = self.secrets.get("proxy_secret") or ""
        register_secret(secret)
        return ProxySettings(
            host=self.repo.get("proxy_host", "") or "",
            port=int(self.repo.get("proxy_port", "443") or 443),
            secret=secret,
            enabled=bool(self.repo.get_json("proxy_enabled", False)),
        )

    def set_proxy(self, p: ProxySettings) -> None:
        register_secret(p.secret)
        self.repo.set("proxy_host", p.host.strip())
        self.repo.set("proxy_port", str(p.port))
        self.repo.set_json("proxy_enabled", p.enabled)
        if p.secret:
            self.secrets.set("proxy_secret", p.secret.strip())
        else:
            self.secrets.delete("proxy_secret")

    # -- Telegram API credentials ----------------------------------------------------------
    # Precedence: developer environment variables -> credentials bundled into the build ->
    # credentials the user typed once and we stored protected. None of them are ever logged.
    # This is the ONLY module that reads the environment variable names, so the regression test
    # that greps app/ for them stays meaningful.
    def get_api_credentials(self) -> tuple[int, str] | None:
        env_id, env_hash = os.environ.get("TELOUDE_API_ID"), os.environ.get("TELOUDE_API_HASH")
        if env_id and env_hash:
            register_secret(env_hash)
            return int(env_id), env_hash
        bundled = bundled_api_credentials()
        if bundled is not None:
            register_secret(bundled[1])
            return bundled
        stored_hash = self.secrets.get("api_hash")
        stored_id = self.repo.get("api_id")
        if stored_hash and stored_id:
            register_secret(stored_hash)
            return int(stored_id), stored_hash
        return None

    def set_api_credentials(self, api_id: int, api_hash: str) -> None:
        register_secret(api_hash)
        self.repo.set("api_id", str(int(api_id)))
        self.secrets.set("api_hash", api_hash.strip())
