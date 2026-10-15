from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "general-document-review"
    data_dir: Path = ROOT / "data"
    static_dir: Path = ROOT / "frontend" / "dist"

    @field_validator("data_dir", "static_dir", mode="after")
    @classmethod
    def resolve_directory(cls, value: Path) -> Path:
        return value if value.is_absolute() else ROOT / value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.sqlite3"

    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    rq_queue: str = "general-document-review"
    rq_job_timeout: int = Field(default=14400, ge=1)
    rq_result_ttl: int = Field(default=3600, ge=1)
    rq_failure_ttl: int = Field(default=86400, ge=1)
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout: int = Field(default=300, ge=1)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_context_chars: int = Field(default=48000, ge=8000)
    agent_concurrency: int = Field(default=2, ge=1, le=16)
    paddleocr_url: str = ""
    paddleocr_token: SecretStr = SecretStr("")
    paddleocr_batch_pages: int = Field(default=10, ge=1, le=100)
    paddleocr_timeout: int = Field(default=180, ge=1)
    max_files: int = Field(default=20, ge=1)
    max_file_mb: int = Field(default=100, ge=1)
    max_unpacked_mb: int = Field(default=1024, ge=1)
    retention_days: int = Field(default=30, ge=1)
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173"
    maintenance_interval: int = Field(default=3600, ge=10)
    sandbox_bwrap_path: str = "/usr/bin/bwrap"
    sandbox_command_timeout: int = Field(default=60, ge=1, le=300)
    sandbox_output_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    sandbox_memory_mb: int = Field(default=512, ge=64, le=4096)
    sandbox_pids_limit: int = Field(default=64, ge=8, le=256)
    sandbox_file_mb: int = Field(default=128, ge=1, le=1024)
    sandbox_workspace_mb: int = Field(default=256, ge=16, le=4096)
    sandbox_officecli_as_mb: int = Field(default=4096, ge=2048, le=16384)
