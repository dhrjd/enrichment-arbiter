"""Deterministic mock adapter — no network, no spend.

Used by ``enrich --dry-run`` and by the test suite as a stand-in. Behaviour is a
pure function of the lookup hash, so a dry run is reproducible.
"""

from __future__ import annotations

import hashlib

from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult


class MockProvider(BaseProvider):
    task_types = (TaskType.work_email.value, TaskType.mobile_phone.value,
                  TaskType.company_firmographics.value)

    def __init__(self, provider_id: str = "mock", match_percent: int = 60, fail: bool = False):
        self.id = provider_id
        self.match_percent = match_percent
        self.fail = fail

    def _roll(self, req: EnrichmentRequest) -> int:
        h = hashlib.sha256(f"{self.id}|{req.lookup_hash()}".encode()).hexdigest()
        return int(h[:8], 16) % 100

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        if self.fail:
            return ProviderResult.error("mock_failure", status_code=503)
        if self._roll(req) >= self.match_percent:
            return ProviderResult(matched=False, status_code=200)

        n = req.normalized()
        if req.task_type == TaskType.work_email:
            first = n["first_name"] or "lead"
            last = n["last_name"] or "person"
            domain = n["domain"] or "example.com"
            value = f"{first}.{last}@{domain}"
            return ProviderResult(matched=True, status_code=200, value=value,
                                  data={"email": value})
        if req.task_type == TaskType.mobile_phone:
            value = "+15550100" + f"{self._roll(req):02d}"
            return ProviderResult(matched=True, status_code=200, value=value,
                                  data={"phone": value})
        data = {
            "name": n["domain"] or "Example",
            "domain": n["domain"] or "example.com",
            "employee_count": 100,
            "industry": "software",
        }
        return ProviderResult(matched=True, status_code=200, value=data["domain"], data=data)
