from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'TG Monitor Platform'
    app_host: str = '127.0.0.1'
    app_port: int = 8098
    app_debug: bool = False

    database_host: str = '127.0.0.1'
    database_port: int = 3306
    database_user: str = 'root'
    database_password: str = ''
    database_name: str = 'tg_monitor'

    telegram_tdata_path: str = ''
    telegram_session_path: str = './data/telethon.session'
    telegram_session_mode: str = 'manual'  # existing|manual
    telegram_desktop_import_enabled: bool = False
    telegram_desktop_import_mode: str = 'create_new'
    telegram_live_listener_enabled: bool = False
    telegram_background_collection_enabled: bool = False
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_proxy_host: str = ''
    telegram_proxy_port: int = 0

    analysis_top_keywords: int = 30
    sync_batch_size: int = 200
    sync_lookback_messages: int = 1000
    sync_interval_minutes: int = 5
    ai_summary_batch_size: int = 1000
    ai_summary_min_batch_size: int = 50
    ai_summary_running_timeout_minutes: int = 30
    ai_summary_slide_window_enabled: bool = True
    ai_summary_slide_window_size: int = 100
    ai_summary_min_trigger_interval_minutes: int = 5
    url_classification_enabled: bool = True
    url_classification_interval_minutes: int = 30
    url_classification_batch_size: int = 50
    key_lead_analysis_enabled: bool = True
    key_lead_analysis_interval_minutes: int = 30
    key_lead_analysis_batch_size: int = 200
    telegram_join_queue_enabled: bool = True
    telegram_join_interval_minutes: int = 10
    telegram_download_media_enabled: bool = False
    telegram_fetch_user_about_enabled: bool = False
    stopwords_extra: str = ''
    media_storage_path: str = './data/media'

    # Auth
    auth_password: str = ''
    api_sk: str = ''

    @field_validator('telegram_api_id', mode='before')
    @classmethod
    def empty_api_id_to_none(cls, value):
        if value in ('', None):
            return None
        return value

    @field_validator('telegram_api_hash', mode='before')
    @classmethod
    def empty_api_hash_to_none(cls, value):
        if value in ('', None):
            return None
        return value

    @field_validator('telegram_session_mode')
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value == 'desktop':
            return 'manual'
        allowed = {'existing', 'manual'}
        if value not in allowed:
            raise ValueError(f'telegram_session_mode must be one of {allowed}')
        return value

    @field_validator('telegram_desktop_import_mode')
    @classmethod
    def validate_desktop_import_mode(cls, value: str) -> str:
        return 'create_new'

    @property
    def sqlalchemy_url(self) -> URL:
        return URL.create(
            'mysql+pymysql',
            username=self.database_user,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            query={'charset': 'utf8mb4'},
        )

    @property
    def admin_sqlalchemy_url(self) -> URL:
        return URL.create(
            'mysql+pymysql',
            username=self.database_user,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database='mysql',
            query={'charset': 'utf8mb4'},
        )

    @property
    def resolved_session_path(self) -> Path:
        path = Path(self.telegram_session_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def resolved_tdata_path(self) -> Path:
        return Path(self.telegram_tdata_path)

    @property
    def resolved_media_storage_path(self) -> Path:
        path = Path(self.media_storage_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path


settings = Settings()
