"""Findymail adapter — email finder (work_email)."""

from __future__ import annotations

import httpx

import config
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult

ENDPOINT = "https://api.findymail.com/api/search/name"
TIMEOUT = 12.0


class FindymailProvider(BaseProvider):
    id = "findymail"
    task_types = (TaskType.work_email.value,)

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        key = config.secret_for_provider(self.id)
        if not key:
            return ProviderResult.error("no_api_key")
        n = req.normalized()
        name = " ".join(p for p in (n["first_name"], n["last_name"]) if p).strip()
        body = {
            "name": name,
            "domain": n["domain"],
            "linkedin_url": n["linkedin_url"] or None,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                ENDPOINT, headers={"Authorization": f"Bearer {key}"}, json=body
            )
        if resp.status_code != 200:
            return ProviderResult(matched=False, status_code=resp.status_code,
                                  error_code=f"http_{resp.status_code}")
        payload = resp.json()
        data = payload.get("contact") or payload.get("data") or payload
        email = data.get("email") if isinstance(data, dict) else None
        if not email:
            return ProviderResult(matched=False, status_code=200)
        return ProviderResult(matched=True, status_code=200, value=email,
                              data={"email": email, "raw": data})
