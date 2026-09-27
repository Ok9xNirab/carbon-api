"""Runtime configuration, loaded from environment variables (prefix ``CARBON_``) and ``.env``."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Env = Literal["local", "prod"]
SearchProvider = Literal["none", "brave", "google"]

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CARBON_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Blank values (e.g. "CARBON_SQS_QUEUE_URL=" copied from .env.example) mean "use default".
        env_ignore_empty=True,
        extra="ignore",
    )

    env: Env = "local"

    # Comma-separated in the environment, e.g. "https://a.com,https://b.com".
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    search_provider: SearchProvider = "none"
    search_api_key: SecretStr | None = None

    dynamodb_table: str = "carbon-local"
    s3_bucket: str = "carbon-artifacts-local"
    sqs_queue_url: str | None = None

    max_words_per_side: int = Field(default=5_000, gt=0)
    max_sources: int = Field(default=10, gt=0)
    max_queries_per_check: int = Field(default=20, gt=0)
    daily_query_cap: int = Field(default=1_000, gt=0)

    # Minimum shingle containment for a candidate source to proceed to alignment.
    containment_threshold: Fraction = 0.05
    # Minimum sentence-embedding cosine similarity to report a paraphrase match.
    paraphrase_threshold: Fraction = 0.80

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _check_prod(self) -> Self:
        if self.env != "prod":
            return self
        if "*" in self.cors_origins:
            raise ValueError("wildcard CORS origin is not allowed in prod")
        if self.search_provider != "none" and self.search_api_key is None:
            raise ValueError(f"search_api_key is required for provider {self.search_provider!r}")
        if not self.sqs_queue_url:
            raise ValueError("sqs_queue_url is required in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings; usable as a FastAPI dependency (``Depends(get_settings)``)."""
    return Settings()
