"""Provider adapter contract.

Every vendor lives in its own module and subclasses ``BaseProvider``. The base
class guarantees the router never sees an exception: timeouts, 429s and 5xx
come back as a clean ``ProviderResult(matched=False, error_code=...)`` with the
latency already measured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from models import EnrichmentRequest


@dataclass
class ProviderResult:
    matched: bool = False
    data: dict | None = None
    value: str | None = None
    status_code: int | None = None
    error_code: str | None = None
    latency_ms: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def is_transport_error(self) -> bool:
        if self.error_code:
            return True
        return self.status_code is not None and (
            self.status_code == 429 or self.status_code >= 500
        )

    @classmethod
    def error(cls, code: str, status_code: int | None = None) -> "ProviderResult":
        return cls(matched=False, error_code=code, status_code=status_code)


class BaseProvider:
    id: str = "base"
    task_types: tuple[str, ...] = ()

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:  # pragma: no cover
        raise NotImplementedError

    async def fetch(self, req: EnrichmentRequest) -> ProviderResult:
        start = time.perf_counter()
        try:
            result = await self._fetch(req)
        except Exception as exc:  # noqa: BLE001 - adapters must never crash the waterfall
            result = ProviderResult.error(f"exception:{type(exc).__name__}")
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        if result.latency_ms <= 0:
            result.latency_ms = 1
        return result


def get_registry() -> dict[str, BaseProvider]:
    """Instantiate every known adapter. Imported lazily to avoid cycles."""
    from providers import apollo, findymail, hunter, prospeo

    instances = [hunter.HunterProvider(), prospeo.ProspeoProvider(),
                 findymail.FindymailProvider(), apollo.ApolloProvider()]
    return {p.id: p for p in instances}
