from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["collecting", "quoted", "rejected", "handoff_pending"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    conversation_id: UUID
    message_id: UUID
    message: str = Field(min_length=1, max_length=4000)


class Handoff(BaseModel):
    id: UUID
    reason: str


class ChatResponse(BaseModel):
    conversation_id: UUID
    message_id: UUID
    correlation_id: UUID
    status: Status
    reply: str
    quote: dict | None = None
    handoff: Handoff | None = None
