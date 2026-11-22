from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MandateCreate(BaseModel):
    agent_id: str = Field(min_length=1, examples=["claude-desktop"])
    owner: str = Field(min_length=1, examples=["akshith"])
    currency: str = Field(default="INR", min_length=3, max_length=3)
    spend_limit_per_week: float = Field(gt=0, examples=[5000])
    approval_threshold: float = Field(ge=0, examples=[1000])
    allowed_merchants: list[str] = Field(min_length=1, examples=[["amazon.in", "swiggy"]])
    allowed_action_types: list[str] = Field(min_length=1, examples=[["purchase"]])
    expires_at: datetime = Field(description="ISO 8601; treated as UTC if no offset")


class Mandate(BaseModel):
    id: str
    agent_id: str
    owner: str
    currency: str
    spend_limit_per_week: float
    approval_threshold: float
    allowed_merchants: list[str]
    allowed_action_types: list[str]
    expires_at: str
    issued_at: str
    revoked: bool
    revoked_at: str | None = None
    signature: str
    signed_body: str
    public_key: str
    signature_valid: bool


class ActionRequest(BaseModel):
    mandate_id: str
    action_type: str = Field(examples=["purchase"])
    merchant: str = Field(examples=["amazon.in"])
    amount: float = Field(gt=0, examples=[1200])


class ActionResponse(BaseModel):
    action_id: str
    decision: Literal["allow", "deny", "pending_approval"]
    reason: str
    approval_id: str | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "deny"]


class Approval(BaseModel):
    approval_id: str
    action_id: str
    mandate_id: str
    status: Literal["pending", "approved", "denied"]
    reason: str | None = None
    action_type: str
    merchant: str
    amount: float
    created_at: str
    decided_at: str | None = None
