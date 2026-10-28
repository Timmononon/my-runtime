"""Proxy fake-IP compatibility without weakening public-destination validation."""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import yaml

from aster.config import load_config
from aster.domain import ToolContext
from aster.tools.http import HTTPTool


def dns_reply(host="example.com", *, addresses=("93.184.216.34",), record_type=1, **overrides):
    return {"Status": 0, "Question": [{"name": host + ".", "type": record_type}],
            "Answer": [{"name": host + ".", "type": record_type, "data": ip} for ip in addresses], **overrides}


def fake_system_dns(monkeypatch, ips=("198.18.1.2",)):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)


def context(tmp_path, *, dns_handler, target_handler, **services):
    return ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services={
        "http_dns_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(dns_handler), follow_redirects=True),
        "http_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(target_handler), follow_redirects=True),
        **services,
    })


async def test_fake_ip_fetch_pins_verified_public_ip_preserves_host_sni_and_extracts(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    dns_calls, target_calls = [], []

    def dns(request):
        dns_calls.append(request)
        assert request.url.host == "dns.google" and request.url.path == "/resolve"
        assert request.url.params["name"] == "example.com" and request.url.params["edns_client_subnet"] == "0.0.0.0/0"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, json=dns_reply())

    def destination(request):
        target_calls.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "example.com" and request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(200, headers={"Content-Type": "text/html"},
            text="<title>Public verification</title><h1>ORCHID 7318</h1><script>hidden</script>")

    ctx = context(tmp_path, dns_handler=dns, target_handler=destination)
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com/page"}, ctx)
    assert not result.is_error, result.content
    assert len(dns_calls) == len(target_calls) == 1
    assert "ORCHID 7318" in result.content and "hidden" not in result.content
    assert result.data["url"] == "https://example.com/page" and result.data["title"] == "Public verification"
    assert len(result.artifacts) == 1


@pytest.mark.parametrize("url,ips,mode", [
    ("https://example.com", ("198.18.1.2",), "off"),
    ("https://198.18.1.2", ("198.18.1.2",), "google"),
    ("https://localhost", ("198.18.1.2",), "google"),
    ("https://intranet", ("198.18.1.2",), "google"),
    ("https://printer.local", ("198.18.1.2",), "google"),
    ("https://router.home.arpa", ("198.18.1.2",), "google"),
    ("https://example.com", ("127.0.0.1",), "google"),
    ("https://example.com", ("10.0.0.1",), "google"),
    ("https://example.com", ("172.16.0.1",), "google"),
    ("https://example.com", ("192.168.1.1",), "google"),
    ("https://example.com", ("169.254.169.254",), "google"),
    ("https://example.com", ("::1",), "google"),
    ("https://example.com", ("198.18.1.2", "10.0.0.1"), "google"),
    ("https://example.com", ("198.18.1.2", "93.184.216.34"), "google"),
])
async def test_private_literal_mixed_and_disabled_destinations_never_use_fallback(tmp_path, monkeypatch, url, ips, mode):
    fake_system_dns(monkeypatch, ips)
    calls = []
    def forbidden(request):
        calls.append(request)
        raise AssertionError("Private/disabled destinations must never be dispatched")
    ctx = context(tmp_path, dns_handler=forbidden, target_handler=forbidden, http_dns_fallback=mode)
    result = await HTTPTool(fetch=True).execute({"url": url}, ctx)
    assert result.is_error and result.error_category == "permission" and result.outcome == "not_executed"
    assert calls == [] and result.artifacts == []


@pytest.mark.parametrize("payload", [
    dns_reply(addresses=("127.0.0.1",)), dns_reply(addresses=("10.0.0.1",)),
    dns_reply(addresses=("169.254.169.254",)), dns_reply(addresses=("198.18.1.2",)),
    dns_reply(addresses=("93.184.216.34", "192.168.1.1")),
])
async def test_public_resolver_private_or_mixed_answer_keeps_guard(tmp_path, monkeypatch, payload):
    fake_system_dns(monkeypatch)
    calls = []
    def target(request):
        calls.append(request)
        return httpx.Response(200)
    ctx = context(tmp_path, dns_handler=lambda request: httpx.Response(200, json=payload), target_handler=target)
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com"}, ctx)
    assert result.is_error and result.error_category == "permission" and result.outcome == "not_executed"
    assert calls == []


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://127.0.0.1/"}),
    httpx.Response(503, text="Never echo resolver bodies or synthetic-dns-secret"),
    httpx.Response(200, text="not-json synthetic-dns-secret"),
    httpx.Response(200, content=b"x" * (64 * 1024 + 1)),
    httpx.Response(200, json=dns_reply(Status=3)),
    httpx.Response(200, json=dns_reply(TC=True)),
    httpx.Response(200, json=dns_reply("other.example")),
    httpx.Response(200, json={"Status": 0, "Answer": []}),
    httpx.Response(200, json=dns_reply(addresses=("synthetic-dns-secret",))),
    httpx.Response(200, json=[]),
])
async def test_unverified_dns_responses_never_send_destination_or_echo(tmp_path, monkeypatch, response):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []
    def dns(request):
        dns_calls.append(request)
        return response
    def target(request):
        targets.append(request)
        return httpx.Response(200)
    ctx = context(tmp_path, dns_handler=dns, target_handler=target)
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com"}, ctx)
    assert result.is_error and result.error_category == "dns" and result.outcome == "not_executed"
    assert len(dns_calls) == 1 and targets == []
    assert "synthetic-dns-secret" not in result.content and not result.artifacts


async def test_dns_failure_write_is_not_dispatched_and_does_not_resend(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    calls = []
    def dns(request):
        calls.append("dns")
        raise httpx.ConnectError("synthetic-dns-secret", request=request)
    def target(request):
        calls.append("target")
        return httpx.Response(200)
    ctx = context(tmp_path, dns_handler=dns, target_handler=target)
    result = await HTTPTool().execute({"url": "https://example.com/action", "method": "POST", "body": "action"}, ctx)
    assert result.is_error and result.outcome == "not_executed" and calls == ["dns"]
    assert "synthetic-dns-secret" not in result.content


async def test_public_dns_does_not_receive_bound_headers_or_follow_private_redirect(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []
    secret = "synthetic-bound-header-secret"
    def dns(request):
        dns_calls.append(request)
        assert secret not in str(request.url) and secret not in str(request.headers)
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(200, json=dns_reply())
    async def resolver(host, port, **kwargs):
        ip = "127.0.0.1" if host == "127.0.0.1" else "198.18.1.2"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    def target(request):
        targets.append(request)
        assert request.headers["Authorization"] == secret
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
    ctx = context(tmp_path, dns_handler=dns, target_handler=target,
        http_credential_origins={"env:BOUND": ["https://example.com"]}, secret_resolver=lambda ref: secret)
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com", "credential_headers": {"Authorization": "env:BOUND"}}, ctx)
    assert result.is_error and len(dns_calls) == len(targets) == 1
    assert secret not in result.content and result.outcome == "known"


async def test_cancellation_during_public_dns_does_not_contact_target(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    entered, cleanup = asyncio.Event(), asyncio.Event()
    calls = []
    async def dns(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
    def target(request):
        calls.append(request)
        return httpx.Response(200)
    ctx = context(tmp_path, dns_handler=dns, target_handler=target)
    task = asyncio.create_task(HTTPTool(fetch=True).execute({"url": "https://example.com"}, ctx))
    await asyncio.wait_for(entered.wait(), 2)
    ctx.cancel.set()
    result = await asyncio.wait_for(task, 2)
    assert result.error_category == "cancelled" and result.outcome != "unknown"
    assert cleanup.is_set() and calls == []


async def test_public_ipv6_fallback_and_ordinary_public_dns_do_not_relax_guards(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    calls = []
    def dns(request):
        kind = int(request.url.params["type"])
        calls.append(kind)
        return httpx.Response(200, json=dns_reply(record_type=kind,
            addresses=() if kind == 1 else ("2606:4700:4700::1111",)))
    def target(request):
        assert request.url.host == "2606:4700:4700::1111" and request.headers["Host"] == "example.com"
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="IPv6 verified")
    ctx = context(tmp_path, dns_handler=dns, target_handler=target)
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com"}, ctx)
    assert not result.is_error and "IPv6 verified" in result.content and calls == [1, 28]
    fake_system_dns(monkeypatch, ("93.184.216.34",))
    ctx.services["http_client_factory"] = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/plain"}, text="System DNS public")))
    result = await HTTPTool(fetch=True).execute({"url": "https://example.com"}, ctx)
    assert not result.is_error and "System DNS public" in result.content and calls == [1, 28]


async def test_timeout_cleans_up_dns_without_dispatching_write(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    cleanup, entered = asyncio.Event(), asyncio.Event()
    calls = []
    async def dns(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
    def target(request):
        calls.append(request)
        return httpx.Response(200)
    ctx = context(tmp_path, dns_handler=dns, target_handler=target)
    result = await HTTPTool().execute({"url": "https://example.com", "method": "POST", "body": "action", "timeout": 0.1}, ctx)
    assert result.is_error and result.error_category == "timeout" and result.outcome != "unknown"
    assert entered.is_set() and cleanup.is_set() and calls == []


@pytest.mark.parametrize("global_mode,agent_mode,expected", [("google", "off", "off"), ("off", "google", "off"), ("google", "google", "google")])
def test_dns_mode_is_snapshot_bound_and_lower_layers_only_disable(tmp_path, global_mode, agent_mode, expected):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"http_dns_fallback": global_mode}), encoding="utf-8")
    snapshot = load_config(tmp_path, overrides={"http_dns_fallback": agent_mode})
    assert snapshot.data["http_dns_fallback"] == expected


@pytest.mark.parametrize("value", [True, None, [], "custom", "https://attacker.example"])
def test_dns_mode_rejects_unknown_resolvers_before_execution(tmp_path, value):
    with pytest.raises(ValueError, match="http_dns_fallback"):
        load_config(tmp_path, overrides={"http_dns_fallback": value})
