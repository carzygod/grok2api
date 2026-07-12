from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AccountStatus = Literal[
    "new",
    "ready",
    "login_required",
    "checking",
    "blocked",
    "error",
    "disabled",
]


class AccountCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    account_id: str = Field(default="", max_length=80)
    cookie_header: str = Field(default="", description="Optional raw Grok request Cookie header.")


class AccountUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    cookie_header: str | None = None


class GenerationQuotaUpdate(BaseModel):
    limit_units: int | None = Field(default=None, ge=0)
    used_units: int | None = Field(default=None, ge=0)
    reserved_units: int | None = Field(default=None, ge=0)
    cooldown_until: int | None = Field(default=None, ge=0)
    cooldown_reason: str | None = Field(default=None, max_length=240)
    reset_used: bool = False


class Account(BaseModel):
    id: str
    name: str
    enabled: bool
    status: AccountStatus
    user_data_dir: str
    browser_container: str = ""
    browser_port: int | None = None
    browser_debug_port: int | None = None
    browser_password: str = ""
    cookie_count: int = 0
    capabilities: list[str] = Field(default_factory=list)
    last_validated_at: int | None = None
    last_error: str = ""
    created_at: int
    updated_at: int


class LoginSession(BaseModel):
    account_id: str
    token: str
    expires_at: int
    browser_url: str
    browser_password: str = ""
    container: str = ""


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]


class ChatCompletionRequest(BaseModel):
    model: str = "grok-web"
    messages: list[ChatMessage]
    stream: bool = False
    account_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class ResponsesRequest(BaseModel):
    model: str = "grok-web"
    input: str | list[Any]
    stream: bool = False
    account_id: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None


class ImageGenerationRequest(BaseModel):
    model: str = "grok-imagine"
    prompt: str
    n: int = 1
    size: str | None = None
    response_format: str | None = None
    image: str | list[str] | None = None
    account_id: str | None = None


class ImageEditRequest(ImageGenerationRequest):
    model: str = "grok-imagine-edit"


class ImageVariationRequest(ImageGenerationRequest):
    model: str = "grok-imagine-variation"
    prompt: str = "Create image variations from the provided reference image."


class VideoGenerationRequest(BaseModel):
    model: str = "grok-video"
    prompt: str
    duration: int | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    image: str | list[str] | None = None
    account_id: str | None = None


class TaskRecord(BaseModel):
    task_id: str
    kind: str
    model: str
    account_id: str | None = None
    status: str
    prompt: str
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: int
    updated_at: int
