"""Prospeo adapter — email finder (work_email)."""

from __future__ import annotations

import httpx

import config
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult

ENDPOINT = "https://api.prospeo.io/email-finder"
TIMEOUT = 12.0


class ProspeoProvider(BaseProvider):
    id = "prospeo"
    task_types = (TaskType.work_email.value,)

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        key = config.secret_for_provider(self.id)
        if not key:
            return ProviderResult.error("no_api_key")
        n = req.normalized()
        body = {
            "first_name": n["first_name"],
            "last_name": n["last_name"],
            "domain": n["domain"],
            "linkedin_url": n["linkedin_url"] or None,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(ENDPOINT, headers={"X-KEY": key}, json=body)
        if resp.status_code != 200:
            return ProviderResult(matched=False, status_code=resp.status_code,
                                  error_code=f"http_{resp.status_code}")
        payload = resp.json()
        if payload.get("error"):
            return ProviderResult(matched=False, status_code=200)
        data = payload.get("response") or payload.get("data") or {}
        email = data.get("email")
        if not email:
            return ProviderResult(matched=False, status_code=200)
        return ProviderResult(matched=True, status_code=200, value=email,
                              data={"email": email, "raw": data})
