"""Provider DNS compatibility through production catalogue and run paths, offline."""
from __future__ import annotations

import asyncio
import json
import os
import socket

import httpx
import pytest
import yaml

from aster.providers.deepseek import ProviderError
from aster.providers.profiles import PublicProviderTransport, verify_public_endpoint
from aster.service import RunCoordinator

HOST = "provider.example"
BASE = f"https://{HOST}/v1"
PUBLIC = "93.184.216.34"
SECRET = "offline-provider-dns-fixture-secret"


def fake_system_dns(monkeypatch, addresses=("198.18.1.74",)):
    async def resolve(host, port, **kwargs):
        assert host == HOST
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)


def dns_response(addresses=(PUBLIC,), *, host=HOST):
    return {"Status": 0, "Question": [{"name": host + ".", "type": 1}],
            "Answer": [{"name": host + ".", "type": 1, "data": ip} for ip in addresses]}


async def mocked_transport(dns, target, *, mode="google"):
    transport = PublicProviderTransport(BASE, dns_fallback=mode,
        dns_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(dns), follow_redirects=True))
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(target)
    return transport


async def test_fake_dns_pins_provider_request_without_forwarding_key_to_resolver(monkeypatch):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []

    def dns(request):
        dns_calls.append(request)
        assert str(request.url).startswith("https://dns.google/resolve?")
        assert request.url.params["name"] == HOST
        assert request.url.params["edns_client_subnet"] == "0.0.0.0/0"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert SECRET not in str(request.url) + str(request.headers)
        return httpx.Response(200, json=dns_response())

    def target(request):
        targets.append(request)
        assert request.url.host == PUBLIC and request.url.path == "/v1/models"
        assert request.headers["Host"] == HOST and request.extensions["sni_hostname"] == HOST
        assert request.headers["Authorization"] == "Bearer " + SECRET
        return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})

    async with httpx.AsyncClient(transport=await mocked_transport(dns, target)) as client:
        response = await client.get(BASE + "/models", headers={"Authorization": "Bearer " + SECRET})
    assert response.status_code == 200 and len(dns_calls) == len(targets) == 1


@pytest.mark.parametrize("addresses,mode", [
    (("198.18.1.74",), "off"),
    (("198.18.1.74",), False),
    (("127.0.0.1",), "google"),
    (("10.0.0.1",), "google"),
    (("169.254.169.254",), "google"),
    (("198.18.1.74", "10.0.0.1"), "google"),
    (("198.18.1.74", PUBLIC), "google"),
])
async def test_private_mixed_and_disabled_provider_dns_never_dispatch(monkeypatch, addresses, mode):
    fake_system_dns(monkeypatch, addresses)
    calls = []

    def forbidden(request):
        calls.append(request)
        raise AssertionError("No resolver or destination request is permitted")

    async with httpx.AsyncClient(transport=await mocked_transport(forbidden, forbidden, mode=mode)) as client:
        with pytest.raises(ProviderError) as error:
            await client.post(BASE + "/chat/completions", json={"content": "private"})
    assert error.value.category == "endpoint" and not error.value.retryable and calls == []


@pytest.mark.parametrize("base", [
    "http://provider.example", "https://198.18.1.74", "https://localhost",
    "https://intranet", "https://printer.local", "https://router.home.arpa", "https://example.test",
])
async def test_unsafe_provider_names_fail_before_any_dns(base):
    def forbidden():
        raise AssertionError("Unsafe provider names must never use the resolver")

    with pytest.raises(ProviderError) as error:
        await verify_public_endpoint(base, dns_fallback="google", dns_client_factory=forbidden)
    assert error.value.category == "endpoint"


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "http://127.0.0.1/stolen"}),
    httpx.Response(503, text=SECRET),
    httpx.Response(200, text=SECRET),
    httpx.Response(200, json=dns_response(("127.0.0.1",))),
    httpx.Response(200, json=dns_response((PUBLIC, "10.0.0.1"))),
    httpx.Response(200, json=dns_response(host="other.example")),
])
async def test_unverified_resolver_never_dispatches_or_echoes(monkeypatch, response):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []

    def dns(request):
        dns_calls.append(request)
        return response

    def target(request):
        targets.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=await mocked_transport(dns, target)) as client:
        with pytest.raises(ProviderError) as error:
            await client.post(BASE + "/chat/completions", headers={"Authorization": "Bearer " + SECRET})
    assert error.value.category == "endpoint" and SECRET not in str(error.value)
    assert len(dns_calls) == 1 and targets == []


async def test_public_dns_path_and_origin_guard_never_use_fallback(monkeypatch):
    fake_system_dns(monkeypatch, (PUBLIC,))
    targets = []

    def dns(request):
        raise AssertionError("Ordinary public DNS must not query DoH")

    def target(request):
        targets.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example/stolen"})

    async with httpx.AsyncClient(transport=await mocked_transport(dns, target), follow_redirects=False) as client:
        response = await client.get(BASE + "/models")
        assert response.status_code == 307
        with pytest.raises(ProviderError):
            await client.get("https://other.example/stolen")
    assert len(targets) == 1 and targets[0].url.host == PUBLIC


@pytest.mark.parametrize("address", [PUBLIC, "2606:4700:4700::1111"])
async def test_public_ip_literals_preserve_direct_transport_without_dns_fallback(monkeypatch, address):
    authority = f"[{address}]" if ":" in address else address
    base = f"https://{authority}/v1"
    targets = []

    async def resolve(host, port, **kwargs):
        assert host == address and port == 443
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def forbidden():
        pytest.fail("Public literals must never query the fallback resolver")

    transport = PublicProviderTransport(base, dns_fallback="google", dns_client_factory=forbidden)
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(lambda request: targets.append(request) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get(base + "/models")).status_code == 200
    assert len(targets) == 1 and targets[0].url.host == address
    assert targets[0].headers["Host"] == authority and targets[0].extensions["sni_hostname"] == address


async def test_cancelled_dns_cleans_up_without_destination_request(monkeypatch):
    fake_system_dns(monkeypatch)
    entered, cleanup = asyncio.Event(), asyncio.Event()
    targets = []

    async def dns(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()

    async with httpx.AsyncClient(transport=await mocked_transport(
            dns, lambda request: targets.append(request) or httpx.Response(200))) as client:
        task = asyncio.create_task(client.post(BASE + "/chat/completions"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cleanup.is_set() and targets == []


def install_transport_fixture(monkeypatch, dns_calls, targets):
    original = PublicProviderTransport.__init__

    def dns(request):
        dns_calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json=dns_response())

    def target(request):
        targets.append(request)
        assert request.url.host == PUBLIC
        assert request.headers["host"] == HOST and request.extensions["sni_hostname"] == HOST
        assert request.headers["authorization"] == "Bearer " + SECRET
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        first = {"choices": [{"index": 0, "delta": {"content": "Fixture answer"}, "finish_reason": None}]}
        final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
            "data: " + json.dumps(first) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n")

    def init(self, base_url, **kwargs):
        original(self, base_url, **kwargs,
            dns_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(dns)))
        # The original unopened pool holds no connections; replace only destination I/O.
        self.inner = httpx.MockTransport(target)

    monkeypatch.setattr(PublicProviderTransport, "__init__", init)


async def test_catalogue_reads_current_global_mode_only_on_fetch(tmp_path, monkeypatch):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []
    install_transport_fixture(monkeypatch, dns_calls, targets)
    coordinator = RunCoordinator(tmp_path, tools=[])
    try:
        item = coordinator.providers.create(name="Fixture", base_url=BASE, protocol="openai_chat", api_key=SECRET)
        assert coordinator.providers.list() and coordinator.providers.get(item["id"])
        assert dns_calls == targets == []
        (tmp_path / "config.yaml").write_text('http_dns_fallback: "off"\n', encoding="utf-8")
        with pytest.raises(ProviderError):
            await coordinator.providers.fetch_models(item["id"], item["revision"])
        assert dns_calls == targets == []
        (tmp_path / "config.yaml").write_text('http_dns_fallback: "google"\n', encoding="utf-8")
        fetched = await coordinator.providers.fetch_models(item["id"], item["revision"])
        assert [model["id"] for model in fetched["models"]] == ["fixture-model"]
        assert len(dns_calls) == len(targets) == 1
    finally:
        await coordinator.close()


@pytest.mark.parametrize("global_mode,agent_mode,expected", [
    ("google", "google", "completed"),
    ("google", "off", "failed"),
    ("off", "google", "failed"),
])
async def test_compatible_run_uses_narrowed_snapshot_dns_mode(tmp_path, monkeypatch, global_mode, agent_mode, expected):
    fake_system_dns(monkeypatch)
    dns_calls, targets = [], []
    install_transport_fixture(monkeypatch, dns_calls, targets)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"http_dns_fallback": global_mode}), encoding="utf-8")
    coordinator = RunCoordinator(tmp_path, tools=[], max_parallel_runs=1)
    try:
        agent_dir = tmp_path / "agents" / "default"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "config.yaml").write_text(yaml.safe_dump({"http_dns_fallback": agent_mode}), encoding="utf-8")
        item = coordinator.providers.create(name="Fixture", base_url=BASE, protocol="openai_chat", api_key=SECRET,
            models=[{"id": "fixture-model"}])
        sid = coordinator.create_session(tmp_path)["id"]
        await coordinator._capacity.acquire()
        run = await coordinator.submit(sid, "Fixture request", provider_id=item["id"], model="fixture-model",
                                       reasoning_effort=None)
        snapshot = coordinator.store.run_request(run["id"])["snapshot"]
        captured_mode = "off" if "off" in (global_mode, agent_mode) else "google"
        assert snapshot["http_dns_fallback"] == captured_mode
        # A queued run retains its captured mode despite subsequent global edits.
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({"http_dns_fallback":
            "off" if captured_mode == "google" else "google"}), encoding="utf-8")
        coordinator._capacity.release()
        result = await coordinator.wait(run["id"], timeout=4)
        assert result.status == expected, result.error
        if expected == "completed":
            assert result.text == "Fixture answer" and len(dns_calls) == len(targets) == 1
            assert targets[0].url.path == "/v1/chat/completions"
        else:
            assert dns_calls == targets == []
    finally:
        if coordinator._capacity.locked():
            coordinator._capacity.release()
        await coordinator.close()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows event-loop resolver integration")
async def test_windows_real_loopback_resolution_cannot_reach_local_listener(monkeypatch):
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    accepted, resolved = [], []

    async def connected(reader, writer):
        accepted.append(True)
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(connected, "127.0.0.1", 0) as server:
        local_port = server.sockets[0].getsockname()[1]

        async def local_resolution(host, port, **kwargs):
            assert host == HOST and port == 443
            # Exercise Windows' actual resolver using only localhost; substitute
            # its answers to reproduce an unsafe provider DNS result offline.
            rows = await original("localhost", local_port, type=socket.SOCK_STREAM)
            resolved.extend(row[4][0] for row in rows)
            return rows

        monkeypatch.setattr(loop, "getaddrinfo", local_resolution)
        with pytest.raises(ProviderError) as error:
            await verify_public_endpoint(BASE, dns_fallback="google",
                dns_client_factory=lambda: pytest.fail("Loopback answers must not query DoH"))
        assert error.value.category == "endpoint"
    assert resolved and all(address in {"127.0.0.1", "::1"} for address in resolved)
    assert accepted == []
