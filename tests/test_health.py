"""Health, metrics and the middleware stack that wraps every response."""

from __future__ import annotations


async def test_liveness_is_public(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readiness_reports_components(client):
    resp = await client.get("/health/ready")
    body = resp.json()
    # Readiness may be degraded (no real Redis) but must still enumerate checks.
    assert resp.status_code in (200, 503)
    assert {c["name"] for c in body["components"]} >= {"postgres", "redis"}


async def test_metrics_exposes_prometheus_text(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


async def test_correlation_id_is_echoed(client):
    resp = await client.get("/health", headers={"X-Correlation-ID": "abc-123"})
    assert resp.headers.get("X-Correlation-ID") == "abc-123"


async def test_security_headers_present(client):
    resp = await client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


async def test_unknown_route_uses_error_envelope(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
