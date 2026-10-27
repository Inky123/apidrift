"""Settings for the GitHub App, read from the environment and a ``.env`` file in the cwd."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = (
    "APIDRIFT_GITHUB_APP_ID",
    "APIDRIFT_GITHUB_APP_SLUG",
    "APIDRIFT_GITHUB_WEBHOOK_SECRET",
    "APIDRIFT_GITHUB_PRIVATE_KEY_PATH",
)
DEFAULT_HOST = "142-93-123-94.sslip.io"
DEFAULT_POLL_MINUTES = 30


@dataclass(frozen=True)
class Settings:
    github_app_id: str
    github_app_slug: str
    github_webhook_secret: str = field(repr=False)  # never in a traceback or log line
    github_private_key_path: Path = field(repr=False)
    host: str = DEFAULT_HOST
    data: Path = Path("data")
    poll_minutes: int = DEFAULT_POLL_MINUTES

    @property
    def db_path(self) -> Path:
        return self.data / "app.db"

    @property
    def work_dir(self) -> Path:
        return self.data / "work"

    @property
    def snapshot_dir(self) -> Path:
        return self.data / "snapshots" / "stripe"

    @property
    def bot_login(self) -> str:
        return f"{self.github_app_slug}[bot]"

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.github_app_slug}/installations/new"

    def private_key(self) -> bytes:
        return self.github_private_key_path.read_bytes()


def load_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Read ``KEY=VALUE`` lines into the environment (existing variables win). Returns what was read."""
    found: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return found
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.isidentifier():
            found[key] = value
            os.environ.setdefault(key, value)
    return found


def from_env(require: bool = True, dotenv: Path | str = ".env") -> Settings:
    """Build Settings; with ``require`` the four GitHub values must be present and the key file must exist."""
    load_dotenv(dotenv)
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if require and missing:
        raise SystemExit("missing configuration: " + ", ".join(missing) + " (run: apidrift app-setup)")
    key_path = Path(env.get("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", ""))
    if require:
        if not key_path.is_file():
            raise SystemExit(f"APIDRIFT_GITHUB_PRIVATE_KEY_PATH does not point at a file: {key_path}")
        try:
            key_path.read_bytes()
        except OSError as exc:
            raise SystemExit(f"cannot read the private key {key_path}: {exc.strerror} (it must be readable by this uid)") from None
    host = env.get("APIDRIFT_HOST", DEFAULT_HOST).strip()
    if not host or "/" in host or ":" in host:
        raise SystemExit(f"APIDRIFT_HOST must be a bare hostname such as apidrift.example.com, not {host!r}")
    raw_minutes = env.get("APIDRIFT_POLL_MINUTES", str(DEFAULT_POLL_MINUTES))
    try:
        poll_minutes = int(raw_minutes)
    except ValueError:
        raise SystemExit(f"APIDRIFT_POLL_MINUTES must be an integer, not {raw_minutes!r}") from None
    if poll_minutes < 1:
        raise SystemExit("APIDRIFT_POLL_MINUTES must be at least 1")
    return Settings(
        github_app_id=env.get("APIDRIFT_GITHUB_APP_ID", ""),
        github_app_slug=env.get("APIDRIFT_GITHUB_APP_SLUG", ""),
        github_webhook_secret=env.get("APIDRIFT_GITHUB_WEBHOOK_SECRET", ""),
        github_private_key_path=key_path,
        host=host,
        data=Path(env.get("APIDRIFT_DATA", "data")),
        poll_minutes=poll_minutes,
    )
