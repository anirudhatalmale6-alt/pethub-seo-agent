from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    AGENT_NAME: str = "seo"
    MANAGER_URL: str = "http://127.0.0.1:8100"
    API_PORT: int = 8101
    WP_URL: str = "https://pethubonline.com"
    WP_USER: str = "jasonsarah2026"
    WP_APP_PASSWORD: str = "EIul 3KqI 3fY7 yLbk Ltva aPnj"
    HEARTBEAT_INTERVAL: int = 120
    AUDIT_INTERVAL_HOURS: int = 24
    DB_PATH: str = "/opt/seo-agent/data/seo_data.json"

    class Config:
        env_file = ".env"


settings = Settings()
