"""Best-effort client IP for anonymous rate limiting and lead attribution.

Render's proxy puts the caller's address in X-Forwarded-For, and
request.client.host is the proxy itself, so the first valid X-Forwarded-For
entry is used, falling back to the direct peer. This is NOT proof of identity:
a caller can prepend its own X-Forwarded-For value. It is therefore only used
where no verified identity exists - logged-in callers are rate-limited by their
signed user id instead (see DivineService.auth.rate_limit_identity).
"""
import ipaddress


def _valid_ip(value: str):
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = _valid_ip(forwarded.split(",", 1)[0])
        if first:
            return first
    peer = getattr(request, "client", None)
    return _valid_ip(getattr(peer, "host", None)) or "unknown"
