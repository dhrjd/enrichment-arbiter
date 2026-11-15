"""Hunter.io adapter — email finder (work_email). Free tier includes API calls."""

from __future__ import annotations

import httpx

import config
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult

ENDPOINT = "https://api.hunter.io/v2/email-finder"
TIMEOUT = 12.0


class HunterProvider(BaseProvider):
    id = "hunter"
    task_types = (TaskType.work_email.value,)

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        key = config.secret_for_provider(self.id)
        if not key:
            return ProviderResult.error("no_api_key")
        n = req.normalized()
        if not n["domain"] or not (n["first_name"] or n["last_name"]):
            return ProviderResult.error("missing_inputs")
        params = {
            "api_key": key,
            "domain": n["domain"],
            "first_name": n["first_name"],
            "last_name": n["last_name"],
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(ENDPOINT, params=params)
        if resp.status_code != 200:
            return ProviderResult(matched=False, status_code=resp.status_code,
                                  error_code=f"http_{resp.status_code}")
        payload = resp.json().get("data") or {}
        email = payload.get("email")
        if not email:
            return ProviderResult(matched=False, status_code=200)
        return ProviderResult(matched=True, status_code=200, value=email,
                              data={"email": email,
                                    "confidence": payload.get("confidence"),
                                    "sources": payload.get("sources")})
