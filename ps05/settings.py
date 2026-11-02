"""Runtime settings for the PS-05 platform.

Reads configuration from the environment (and a local ``.env`` file when
present) into a pydantic-settings ``BaseSettings`` model. Field names mirror
``.env.example``; env vars may carry either the plain name or the ``PS05_``
prefixed form used by the example file.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _alias(*names: str):
    """Build a validation-alias accepting any of the given env var names."""
    return AliasChoices(*names)


class Settings(BaseSettings):
    """Central configuration.

    All values can be overridden via environment variables; a ``.env`` file in
    the working directory is loaded when present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # Core application settings
    env: str = Field(default="development", validation_alias=_alias("PS05_ENV", "ENV"))
    debug: bool = Field(default=False, validation_alias=_alias("PS05_DEBUG", "DEBUG"))
    host: str = Field(default="127.0.0.1", validation_alias=_alias("PS05_HOST", "HOST"))
    port: int = Field(default=8000, validation_alias=_alias("PS05_PORT", "PORT"))
    secret_key: str = Field(
        default="change-this-in-production-to-a-secure-random-64-character-key",
        validation_alias=_alias("PS05_SECRET_KEY", "SECRET_KEY"),
    )

    # Storage & WORM vault
    storage_backend: Literal["posix_worm", "s3"] = Field(
        default="posix_worm",
        validation_alias=_alias("PS05_STORAGE_BACKEND", "STORAGE_BACKEND"),
    )
    worm_root_dir: Path = Field(
        default=Path("./data/vault_worm"),
        validation_alias=_alias("PS05_WORM_ROOT_DIR", "WORM_ROOT_DIR"),
    )
    derived_artifacts_dir: Path = Field(
        default=Path("./data/derived_cache"),
        validation_alias=_alias("PS05_DERIVED_ARTIFACTS_DIR", "DERIVED_ARTIFACTS_DIR"),
    )

    # S3 / MinIO
    s3_endpoint: str = Field(
        default="http://127.0.0.1:9000",
        validation_alias=_alias("PS05_S3_ENDPOINT", "S3_ENDPOINT"),
    )
    s3_access_key: str = Field(
        default="minioadmin",
        validation_alias=_alias("PS05_S3_ACCESS_KEY", "S3_ACCESS_KEY"),
    )
    s3_secret_key: str = Field(
        default="minioadmin",
        validation_alias=_alias("PS05_S3_SECRET_KEY", "S3_SECRET_KEY"),
    )
    s3_bucket_evidence: str = Field(
        default="evidence-worm",
        validation_alias=_alias("PS05_S3_BUCKET_EVIDENCE", "S3_BUCKET_EVIDENCE"),
    )
    s3_bucket_artifacts: str = Field(
        default="derived-artifacts",
        validation_alias=_alias("PS05_S3_BUCKET_ARTIFACTS", "S3_BUCKET_ARTIFACTS"),
    )

    # Database
    database_url: str | None = Field(
        default=None,
        validation_alias=_alias("PS05_DATABASE_URL", "DATABASE_URL"),
    )
    database_echo: bool = Field(
        default=False,
        validation_alias=_alias("PS05_DATABASE_ECHO", "DATABASE_ECHO"),
    )

    # Task queue
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        validation_alias=_alias("PS05_REDIS_URL", "REDIS_URL"),
    )
    celery_concurrency: int = Field(
        default=4,
        validation_alias=_alias("PS05_CELERY_CONCURRENCY", "CELERY_CONCURRENCY"),
    )

    # ML runtime
    device: str = Field(
        default="cuda",
        validation_alias=_alias("PS05_DEVICE", "DEVICE"),
    )
    fallback_to_cpu: bool = Field(
        default=True,
        validation_alias=_alias("PS05_FALLBACK_TO_CPU", "FALLBACK_TO_CPU"),
    )
    models_cache_dir: Path = Field(
        default=Path("./models/checkpoints"),
        validation_alias=_alias("PS05_MODELS_CACHE_DIR", "MODELS_CACHE_DIR"),
    )

    # Forensic reporting & legal compliance
    legal_jurisdiction: str = Field(
        default="INDIA_BSA_2023",
        validation_alias=_alias("PS05_LEGAL_JURISDICTION", "LEGAL_JURISDICTION"),
    )
    default_certificate_type: str = Field(
        default="SECTION_63_BSA",
        validation_alias=_alias("PS05_DEFAULT_CERTIFICATE_TYPE", "DEFAULT_CERTIFICATE_TYPE"),
    )
    digital_signature_key_path: Path = Field(
        default=Path("./config/certs/examiner_dummy.pem"),
        validation_alias=_alias(
            "PS05_DIGITAL_SIGNATURE_KEY_PATH", "DIGITAL_SIGNATURE_KEY_PATH"
        ),
    )

    @property
    def effective_database_url(self) -> str:
        """Resolved database URL; falls back to in-memory SQLite for tests."""
        return self.database_url or "sqlite+aiosqlite:///:memory:"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached ``Settings`` instance."""
    return Settings()
