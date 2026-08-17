"""
Lambda@Edge viewer-request handler for the vanity-URL redirect POC.

CloudFront invokes this NATIVELY at the edge (no Function URL / no OAC), and we
return the 302 directly at viewer-request -- the origin is never contacted.

New to any of these terms (Lambda@Edge, viewer-request, OAC, SNI, HSTS)? They are
defined in plain language in the README's "Key concepts" glossary.

Differences vs the Function-URL handler:
  - Event shape is the CloudFront event (event['Records'][0]['cf']['request']),
    with headers as lists of {key, value}.
  - Lambda@Edge does NOT support environment variables, so the table name and
    region are hardcoded constants (set to match the CDK stack).
  - DynamoDB is regional; the replicated edge function calls back to us-east-1.
    Fine for a POC; use a Global Table for production low-latency.

Security controls (threat-model driven):
  - Open-redirect protection (T1/T8): only absolute HTTPS targets are honored,
    and when a row carries an `allowedHosts` list the target host must match it
    (per-tenant destination allowlist).
  - Host-spoofing protection (T3): the `x-vanity-host` test header is honored
    ONLY on the default *.cloudfront.net domain; on real custom domains the
    signed `Host` header is authoritative.
  - Input validation (T11): host is length- and charset-bounded before lookup.
  - Log minimization (T6): only the target HOST (not the full URL/query) is
    logged, avoiding leakage of sensitive path/query data.
  - HSTS on every response.
"""

import json
import string
from urllib.parse import urlparse

import boto3

TABLE_NAME = "vanity-redirect-sample"   # must match CoreStack table_name
REGION = "us-east-1"
HSTS_VALUE = "max-age=31536000; includeSubDomains"

# Only trust the x-vanity-host test header when served from the default
# CloudFront domain; real tenant domains must rely on the signed Host header.
TEST_HOST_SUFFIX = ".cloudfront.net"

# RFC-1123-ish hostname bound: labels of a-z/0-9/-, max total length 253.
MAX_HOST_LEN = 253
MAX_LABEL_LEN = 63
_LABEL_CHARS = frozenset(string.ascii_lowercase + string.digits + "-")

_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def _get_header(headers, name):
    h = headers.get(name.lower())
    if h and len(h) > 0:
        return h[0]["value"]
    return ""


def _resolve_host(headers):
    """Return the lookup host.

    On the default *.cloudfront.net domain the x-vanity-host header is honored
    for testing. On any real custom domain the signed Host header wins, so an
    attacker cannot inject x-vanity-host to reach another tenant's mapping.
    """
    real_host = _get_header(headers, "host").split(":")[0].lower()
    if real_host.endswith(TEST_HOST_SUFFIX):
        vanity = _get_header(headers, "x-vanity-host").split(":")[0].lower()
        if vanity:
            return vanity
    return real_host


def _valid_label(label):
    return (
        1 <= len(label) <= MAX_LABEL_LEN
        and label[0] != "-"
        and label[-1] != "-"
        and set(label) <= _LABEL_CHARS
    )


def _valid_host(host):
    """Linear-time RFC-1123-ish hostname check (no regex -> no ReDoS).

    Bounded by MAX_HOST_LEN overall and MAX_LABEL_LEN per label; each label is
    lowercase alnum/hyphen and must not start or end with a hyphen. Host is
    already lowercased by _resolve_host.
    """
    if not host or len(host) > MAX_HOST_LEN:
        return False
    return all(_valid_label(label) for label in host.split("."))


def _target_host_allowed(target_netloc, allowed_hosts):
    """Per-tenant destination allowlist.

    If the row carries `allowedHosts`, the target host must match one entry
    (exact or dot-suffix). If the attribute is absent, the allowlist is not
    configured for this tenant and the check is skipped (https-only still
    applies). Production rows SHOULD populate allowedHosts.
    """
    if not allowed_hosts:
        return True
    host = target_netloc.split(":")[0].lower()
    for entry in allowed_hosts:
        a = str(entry).lower().lstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


def _is_safe_target(url, allowed_hosts=None):
    try:
        p = urlparse(url)
    except Exception:
        return False
    # HTTPS-only: no plain-http downgrade targets.
    if p.scheme != "https" or not p.netloc:
        return False
    return _target_host_allowed(p.netloc, allowed_hosts)


def _target_host(url):
    """Hostname only, for safe logging (no path/query)."""
    try:
        return urlparse(url).netloc.split(":")[0].lower()
    except Exception:
        return ""


def _resp(status, description, extra_headers=None):
    headers = {
        "strict-transport-security": [
            {"key": "Strict-Transport-Security", "value": HSTS_VALUE}
        ],
        "content-type": [{"key": "Content-Type", "value": "text/plain"}],
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"status": str(status), "statusDescription": description, "headers": headers}


def handler(event, _context):
    request = event["Records"][0]["cf"]["request"]
    headers = request.get("headers", {})
    host = _resolve_host(headers)

    # Input validation (T11): reject empty/oversized/malformed hosts early.
    if not _valid_host(host):
        return _resp(400, "Bad Request")

    try:
        item = _table.get_item(Key={"host": host}).get("Item")
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"level": "error", "msg": "ddb_error", "host": host, "err": str(exc)}))
        return _resp(500, "Internal Server Error")

    if not item or item.get("status") != "active":
        print(json.dumps({"level": "info", "msg": "no_mapping_or_inactive", "host": host}))
        return _resp(403, "Forbidden")

    target = item.get("targetUrl", "")
    allowed_hosts = item.get("allowedHosts")  # optional per-tenant allowlist
    if not _is_safe_target(target, allowed_hosts):
        # Log only the target host, never the full URL/query (T6).
        print(json.dumps({
            "level": "warn",
            "msg": "unsafe_target_blocked",
            "host": host,
            "target_host": _target_host(target),
        }))
        return _resp(403, "Forbidden")

    # Log minimization (T6): record the target HOST, not the full URL/query.
    print(json.dumps({"level": "info", "msg": "redirect", "host": host, "target_host": _target_host(target)}))
    return _resp(
        302,
        "Found",
        {
            "location": [{"key": "Location", "value": target}],
            "cache-control": [{"key": "Cache-Control", "value": "no-store"}],
        },
    )
