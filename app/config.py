from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    USE_MOCKS: bool = True
    NOW_OVERRIDE: str = ""
    POLLER_ENABLED: bool = False
    DEMO_PHONE: str = "+15555550100"
    ANTHROPIC_API_KEY: str = ""
    DIAL_API_KEY: str = ""
    DIAL_NUMBER: str = ""
    DIAL_FROM_NUMBER_ID: str = ""
    TIMO_NUMBER: str = "+15555550199"
    USER_NUMBER: str = ""
    USER_NUMBER_ID: str = ""
    DIAL_SMS_WEBHOOK_SECRET: str = ""
    MOCK_DIAL: bool = True
    DARWIN_API_KEY: str = ""
    GOOGLE_TOKEN_PATH: str = ""
    TODOIST_TOKEN: str = ""
    LLM_MODEL: str = "claude-haiku-5-5"


settings = Settings()
