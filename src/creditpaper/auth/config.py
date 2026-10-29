from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import model_validator
from pydantic_settings import BaseSettings

from creditpaper.env_file import env_file_path

# Same rule as db.py and alembic/env.py: APP_ENV=local reads `.env.local`, otherwise `.env`.
_ENV_FILE = str(env_file_path())


class Settings(BaseSettings):
    APP_ENV: str = "production"
    DATABASE_URL: str  # required — must be set in .env
    JWT_SECRET_KEY: str = "super-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@creditpaper.com"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    SUPABASE_MICROSOFT_TENANT_ID: str = ""

    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "http://localhost:8000/api/auth/github/callback"

    FRONTEND_URL: str = "http://localhost:5173"
    # Send session and OAuth cookies over HTTPS only. Must be true anywhere but local development.
    COOKIE_SECURE: bool = False

    ENCRYPTION_KEY: str = ""
    model_config = {"env_file": _ENV_FILE, "extra": "ignore", "hide_input_in_errors": True}

    @model_validator(mode="after")
    def production_security(self):
        if self.APP_ENV not in {"local", "test"}:
            if (
                len(self.JWT_SECRET_KEY) < 32
                or self.JWT_SECRET_KEY == "super-secret-key-change-in-production"
            ):
                raise ValueError("Production requires a strong, explicitly configured JWT signing secret")
            if not self.FRONTEND_URL.startswith("https://"):
                raise ValueError("Production requires an HTTPS frontend origin")
            if not self.COOKIE_SECURE:
                raise ValueError("Production requires COOKIE_SECURE=true")
        return self

    @classmethod
    def get_database_url(cls, url: str) -> str:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        if "sslmode" in params:
            sslmode = params.pop("sslmode")[0]
            if sslmode in ("require", "prefer", "allow"):
                params["ssl"] = ["require"]

        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))


@lru_cache
def get_settings() -> Settings:
    return Settings()
