from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment-supplied identity and browser origin configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    oidc_issuer: str = Field(min_length=1)
    oidc_audience: str = Field(min_length=1)
    oidc_public_key: str = Field(min_length=1, repr=False)
    allowed_origins: tuple[str, ...] = Field(min_length=1)

    @field_validator("allowed_origins")
    @classmethod
    def allowed_origins_must_be_normalized(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            try:
                parsed = urlsplit(origin)
                port = parsed.port
            except ValueError as error:
                raise ValueError("allowed origins must be valid absolute origins") from error
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or "*" in parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed origins must be normalized absolute origins")
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            default_port = 443 if parsed.scheme == "https" else 80
            normalized = f"{parsed.scheme}://{host}"
            if port is not None and port != default_port:
                normalized = f"{normalized}:{port}"
            if origin != normalized:
                raise ValueError("allowed origins must be normalized absolute origins")
        return origins
