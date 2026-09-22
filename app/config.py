from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment-supplied identity and browser origin configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    oidc_issuer: str = Field(min_length=1)
    oidc_audience: str = Field(min_length=1)
    oidc_public_key: str = Field(min_length=1, repr=False)
    allowed_origins: tuple[str, ...] = Field(min_length=1)
