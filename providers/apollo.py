"""Apollo.io adapter — people match (work_email).

Note: Apollo's API is generally gated behind a paid plan. The adapter is here so
a key that *does* have API access can be dropped in; without one it reports
``no_api_key`` and the waterfall skips it cleanly.
"""

from __future__ import annotations

import httpx

import config
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult

ENDPOINT = "https://api.apollo.io/v1/people/match"
TIMEOUT = 15.0


class ApolloProvider(BaseProvider):
    id = "apollo"
    task_types = (TaskType.work_email.value,)

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        key = config.secret_for_provider(self.id)
        if not key:
            return ProviderResult.error("no_api_key")
        n = req.normalized()
        body = {
            "api_key": key,
            "first_name": n["first_name"] or None,
            "last_name": n["last_name"] or None,
            "domain": n["domain"] or None,
            "organization_domain": n["domain"] or None,
            "linkedin_url": n["linkedin_url"] or None,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(ENDPOINT, json=body)
        if resp.status_code != 200:
            return ProviderResult(matched=False, status_code=resp.status_code,
                                  error_code=f"http_{resp.status_code}")
        person = resp.json().get("person") or {}
        email = person.get("email")
        if not email:
            return ProviderResult(matched=False, status_code=200)
        return ProviderResult(matched=True, status_code=200, value=email,
                              data={"email": email,
                                    "name": person.get("name"),
                                    "title": person.get("title")})
