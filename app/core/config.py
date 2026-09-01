from __future__ import annotations

from functools import lru_cache
import ipaddress
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "静页书房"
    app_env: str = "development"
    debug: bool = True
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'local.db').as_posix()}"
    public_base_url: str = "http://127.0.0.1:8000"
    session_secret: str = "dev-only-change-before-production"
    session_https_only: bool = False
    local_storage_root: Path = PROJECT_ROOT / "runtime"
    default_admin_username: str = "admin"
    default_admin_password: str = "ChangeMe123!"
    import_max_rows: int = Field(default=20000, ge=1, le=100000)
    import_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    link_check_timeout_seconds: float = Field(default=8.0, ge=1, le=30)
    link_check_automatic_enabled: bool = True
    link_check_interval_minutes: int = Field(default=360, ge=5, le=10080)
    link_check_poll_seconds: int = Field(default=60, ge=10, le=3600)
    link_check_batch_size: int = Field(default=10, ge=1, le=100)
    link_check_error_threshold: int = Field(default=2, ge=1, le=10)
    cloud_upload_worker_enabled: bool = True
    cloud_upload_poll_seconds: int = Field(default=2, ge=1, le=60)
    cloud_upload_max_scan_files: int = Field(default=1000, ge=1, le=5000)
    cloud_upload_timeout_seconds: int = Field(default=6 * 60 * 60, ge=60, le=24 * 60 * 60)
    cloud_upload_source_roots: str = ""
    cloud_node_executable: Path | None = None
    baidu_netdisk_access_token: str | None = Field(default=None, repr=False)
    baidu_netdisk_client_id: str | None = None
    baidu_netdisk_remote_dir: str = "/静页书房"
    quark_cli_path: Path | None = None
    quark_skill_config_url: str = "https://open-api-drive.quark.cn/agent/v1/skill_config"
    organizer_site_id: str = "jingye-local"
    organizer_cover_hosts: str = ""
    site_aliases: str = ""
    maintenance_control_root: Path | None = None
    organizer_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"development", "test", "production"}:
            raise ValueError("APP_ENV 只能是 development、test 或 production")
        return normalized

    def validate_runtime_safety(self) -> None:
        if self.app_env == "production" and self.session_secret == "dev-only-change-before-production":
            raise RuntimeError("生产环境必须设置强随机 SESSION_SECRET")
        if self.app_env == "production" and (len(self.session_secret) < 32 or self.debug or not self.session_https_only):
            raise RuntimeError("生产环境必须设置至少32位随机会话密钥、DEBUG=false 和 SESSION_HTTPS_ONLY=true，并启用 HTTPS")
        if self.app_env == "production":
            url = urlsplit(self.public_base_url)
            host = url.hostname or ""
            try:
                ipaddress.ip_address(host)
                is_ip = True
            except ValueError:
                is_ip = False
            if (url.scheme != "https" or "." not in host or is_ip or url.username or url.password
                    or url.query or url.fragment or url.path not in {"", "/"}
                    or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))):
                raise RuntimeError("生产环境 PUBLIC_BASE_URL 必须填写正式 HTTPS 域名，不能保留本机地址或包含路径")

    def upload_source_roots(self) -> list[Path]:
        return [Path(item.strip()).resolve() for item in self.cloud_upload_source_roots.split(";") if item.strip()]

    def baidu_authorize_url(self) -> str:
        client_id = self.baidu_netdisk_client_id or "QHOuRXiepJBMjtk0esLhrPoNlQyYd0mF"
        return "https://openapi.baidu.com/oauth/2.0/authorize?" + urlencode(
            {
                "response_type": "token",
                "client_id": client_id,
                "redirect_uri": "oob",
                "scope": "basic,netdisk",
            }
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime_safety()
    settings.local_storage_root.mkdir(parents=True, exist_ok=True)
    return settings
