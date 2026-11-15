"""Task-dispatched verification.

Default is free/local: email syntax + DNS MX (falling back to A). No port-25
SMTP probes (they fail from most cloud/VPS IPs). A paid HTTP verifier can be
added later behind an env flag without touching the router.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

MX_TIMEOUT = 5.0

FIRMOGRAPHICS_REQUIRED = ("name", "domain")


def email_syntax_ok(email: str) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email))


async def default_mx_lookup(domain: str) -> bool:
    """True when the domain has an MX (or at least an A) record."""
    try:
        import dns.asyncresolver
        import dns.resolver
    except ImportError:  # pragma: no cover - dnspython is a declared dep
        return True

    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = MX_TIMEOUT
    try:
        answers = await resolver.resolve(domain, "MX")
        return len(answers) > 0
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        pass
    except Exception:  # noqa: BLE001 - timeouts / no nameservers
        return False
    try:
        answers = await resolver.resolve(domain, "A")
        return len(answers) > 0
    except Exception:  # noqa: BLE001
        return False


async def verify(task_type, value, payload=None, *, mx_lookup=None) -> bool:
    task = getattr(task_type, "value", task_type)
    if task == "work_email":
        email = value or (payload or {}).get("email") or ""
        if not email_syntax_ok(email):
            return False
        domain = email.rsplit("@", 1)[1]
        lookup = mx_lookup or default_mx_lookup
        return bool(await lookup(domain))
    if task == "mobile_phone":
        phone = value or (payload or {}).get("phone") or ""
        digits = re.sub(r"\D", "", phone)
        return 7 <= len(digits) <= 15
    if task == "company_firmographics":
        data = payload or {}
        if not all(data.get(k) for k in FIRMOGRAPHICS_REQUIRED):
            return False
        return bool(data.get("employee_count") or data.get("industry"))
    return False
