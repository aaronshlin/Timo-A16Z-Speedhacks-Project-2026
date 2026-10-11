import re
from pydantic import Field, field_validator
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
    DARWIN_API_KEY: str = ""
    GOOGLE_TOKEN_PATH: str = ""
    TODOIST_TOKEN: str = ""
    LLM_MODEL: str = "claude-haiku-5-5"
    LIVE_CALLS_ENABLED: bool = False
    CALL_OPT_IN: bool = False
    CALL_TRIGGER_TOKEN: str = ""
    CALENDAR_TRIGGER_TOKEN: str = ""
    DIAL_WEBHOOK_SECRET: str = ""
    DIAL_FROM_NUMBER_ID: str = "cmv44iugl00mt0ftbpptzmudz"
    CALL_SESSION_LIMIT: int = Field(default=1, ge=1)
    CALL_COOLDOWN_SECONDS: int = Field(default=300, ge=0)
    CALL_STATE_PATH: str = ""
    CALL_ENGINE_STATE_PATH: str = ""
    GOOGLE_CALENDAR_IDS: list[str] = ["primary"]
    CALENDAR_ACTIVE_START_HOUR: int = 9
    CALENDAR_ACTIVE_END_HOUR: int = 18
    CALENDAR_BUFFER_MINUTES: int = Field(default=3, ge=0, le=30)
    RECEIPT_MODE: str = "dashboard"
    # The second Dial number, which plays the user (phone/test_user.py). DEMO_PHONE is its E.164.
    DIAL_USER_NUMBER_ID: str = ""
    # Real outbound SMS through Dial. Off: texts land in the mock outbox.
    SMS_ENABLED: bool = False
    # Static bearer token Dial's Context MCP sends to /mcp.
    MCP_TOKEN: str = ""

    @field_validator("DIAL_NUMBER")
    @classmethod
    def e164_number(cls, value):
        if value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("DIAL_NUMBER is E.164; use DIAL_FROM_NUMBER_ID for a Dial number ID")
        return value


settings = Settings()
