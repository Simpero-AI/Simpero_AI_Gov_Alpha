from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    alembic_database_url: str
    clerk_secret_key: str
    clerk_jwks_url: str
    valkey_url: str
    voyage_api_key: str = ""
    embedding_model: str = "voyage-4-large"
    # Must match the chunks.embedding column dim (Vector(1024)); changing it
    # needs a matching migration. voyage-4 serves 2048/1024/512/256.
    embedding_dimension: int = 1024
    environment: str = "development"
    cors_allowed_origins: str = "http://localhost:3000"
    # Clerk org id of the Simpero platform org (internal staff). "" => the
    # platform-admin surface fails closed (denies everyone) until set.
    simpero_platform_org_id: str = ""
    # SEC EDGAR entity resolution (SIM-262). Keyless — SEC asks only for a
    # descriptive User-Agent naming a contact, e.g. "Simpero AI ops@simpero.ai".
    # "" => EdgarResolver raises at construction rather than sending
    # unidentified traffic SEC answers with 403, so the feature fails closed
    # until set — same posture as voyage_api_key.
    sec_edgar_user_agent: str = ""
    # Frontend base URL — builds Clerk invitation redirect_url(s) for both the
    # admin seed invite (/admin/sign-up) and product-user invites (/sign-up).
    app_base_url: str = "http://localhost:3000"
    # D3 downgrade-sync grace window (see _ensure_admin_provisioned): a row
    # this app just promoted/reactivated is exempt from the downgrade-only
    # sync for this many seconds, covering the window before a caller's
    # already-issued session JWT refreshes to reflect the new org_role.
    admin_role_sync_grace_seconds: int = 120
    # Document-upload Spaces settings. Reuses the parser service's PARSER_SPACES_*
    # env vars directly (same bucket/credentials, decided by Vansh) rather than
    # duplicating identical values under a second name — no ParserSettings class
    # exists in this app (parsing moved to Simpero_Gov_AI_Services), so there's
    # nothing to actually import; aliasing the env var is the whole story.
    spaces_bucket: str = Field(default="", validation_alias="PARSER_SPACES_BUCKET")
    spaces_region: str = Field(default="", validation_alias="PARSER_SPACES_REGION")
    spaces_endpoint_url: str = Field(default="", validation_alias="PARSER_SPACES_ENDPOINT_URL")
    spaces_access_key_id: str = Field(default="", validation_alias="PARSER_SPACES_ACCESS_KEY_ID")
    spaces_secret_access_key: str = Field(
        default="", validation_alias="PARSER_SPACES_SECRET_ACCESS_KEY"
    )

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    # lru_cache: Settings are immutable at runtime; re-reading .env on every call is wasteful
    return Settings()  # type: ignore[call-arg]  # pydantic-settings populates fields from env at runtime
