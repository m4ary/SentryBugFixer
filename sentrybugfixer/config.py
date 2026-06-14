"""Runtime configuration, read from environment / .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env sitting next to the project root (SentryBugFixer/.env).
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


class Settings:
    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("SBF_MODEL", "claude-opus-4-8")
    effort: str = os.getenv("SBF_EFFORT", "high")  # low | medium | high | xhigh | max
    request_timeout: float = float(os.getenv("SBF_REQUEST_TIMEOUT", "300"))

    # Sentry
    sentry_token: str = os.getenv("SENTRY_AUTH_TOKEN", "")
    sentry_host: str = os.getenv("SENTRY_HOST", "https://sentry.io").rstrip("/")
    sentry_webhook_secret: str = os.getenv("SENTRY_WEBHOOK_SECRET", "")

    # GitLab
    gitlab_token: str = os.getenv("GITLAB_TOKEN", "")
    gitlab_host: str = os.getenv("GITLAB_HOST", "https://gitlab.com").rstrip("/")

    # Agent limits
    max_steps: int = int(os.getenv("SBF_MAX_STEPS", "40"))
    bash_timeout: int = int(os.getenv("SBF_BASH_TIMEOUT", "180"))

    # Server / storage
    host: str = os.getenv("SBF_HOST", "127.0.0.1")
    port: int = int(os.getenv("SBF_PORT", "8000"))
    data_dir: Path = Path(os.getenv("SBF_DATA_DIR", "./data")).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sentrybugfixer.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
