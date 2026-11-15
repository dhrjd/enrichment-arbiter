"""Unified Pydantic v2 request/response schemas."""

from __future__ import annotations

import hashlib
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TaskType(str, Enum):
    work_email = "work_email"
    mobile_phone = "mobile_phone"
    company_firmographics = "company_firmographics"


def normalize_domain(domain: str | None) -> str:
    if not domain:
        return ""
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/", 1)[0]
    return d.lstrip("www.") if d.startswith("www.") else d


class EnrichmentRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_type: TaskType
    segment: str = "global"
    first_name: str | None = None
    last_name: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    max_cost_budget: float | None = Field(default=None, ge=0)
    request_id: str | None = None

    def normalized(self) -> dict:
        return {
            "task_type": self.task_type.value,
            "segment": (self.segment or "global").strip().lower() or "global",
            "first_name": (self.first_name or "").strip().lower(),
            "last_name": (self.last_name or "").strip().lower(),
            "domain": normalize_domain(self.domain),
            "linkedin_url": (self.linkedin_url or "").strip().lower().rstrip("/"),
            "email": (self.email or "").strip().lower(),
        }

    def lookup_input(self) -> str:
        """The identity that determines cache key. Task-specific."""
        n = self.normalized()
        if self.task_type == TaskType.work_email:
            if n["email"]:
                return f"email:{n['email']}"
            if n["linkedin_url"]:
                return f"li:{n['linkedin_url']}"
            return f"name:{n['first_name']}|{n['last_name']}|{n['domain']}"
        if self.task_type == TaskType.mobile_phone:
            if n["linkedin_url"]:
                return f"li:{n['linkedin_url']}"
            if n["email"]:
                return f"email:{n['email']}"
            return f"name:{n['first_name']}|{n['last_name']}|{n['domain']}"
        return f"domain:{n['domain']}"

    def lookup_hash(self) -> str:
        raw = f"{self.task_type.value}|{self.lookup_input()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TraceStep(BaseModel):
    provider_id: str
    status: str                       # matched | miss | error | skipped_budget | cooldown
    matched: bool = False
    verified: bool = False
    latency_ms: int = 0
    cost: float = 0.0
    error_code: str | None = None
    explore: bool = False


class EnrichmentResponse(BaseModel):
    success: bool
    data: dict | None = None
    provider_used: str | None = None
    waterfall_trace: list[TraceStep] = Field(default_factory=list)
    total_cost: float = 0.0
    cached: bool = False
    negative: bool = False
