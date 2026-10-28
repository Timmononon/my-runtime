"""Bounded HTTP tools with pinned DNS, explicit credentials and no automatic replay."""
from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import re
import socket
import uuid
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from jsonschema import validate

from aster.customization.skills import CredentialResolver
from aster.domain import ToolResult, ToolSpec
from aster.platform.filesystem import atomic_write
from aster.tools.files import TEXT, artifact, bounded_result, failure, read_path, schema

SECRET_NAME = re.compile(r"authorization|cookie|api[-_]?key|token|secret|password|credential|signature", re.IGNORECASE)
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "proxy-connection"}
PRIVATE_CREDENTIALS = {"env:deepseek_api_key", "keyring:aster/deepseek", "keyring:deepseek"}
SENSITIVE_HEADER_PATTERN = "".join(f"[{char.lower()}{char.upper()}]" if char.isalpha() else char
                                  for char in "authorization|cookie|api[-_]?key|token|secret|password|credential|signature")
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
PUBLIC_DNS_ENDPOINT = "https://dns.google/resolve"


class PublicDNSFailure(Exception):
    """Fixed, credential-free failure before the destination request."""


async def public_dns_addresses(host: str, *, client_factory=None) -> list[str]:
    """Verify proxy fake-IP names through fixed HTTPS DNS; never reuse credentials."""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False))
    try:
        async with factory() as client:
            for record_type, version in ((1, 4), (28, 6)):
                async with client.stream("GET", PUBLIC_DNS_ENDPOINT,
                        params={"name": host, "type": str(record_type), "edns_client_subnet": "0.0.0.0/0"},
                        headers={"Accept": "application/dns-json"}, follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise PublicDNSFailure("Proxy DNS returned virtual addresses; public DNS verification failed")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        # A DNS reply is small; bound untrusted resolver output
                        # inside the HTTP tool's existing timeout/cancellation.
                        if len(raw) + len(chunk) > 64 * 1024:
                            raise PublicDNSFailure("Public DNS response exceeded the verification limit")
                        raw.extend(chunk)
                payload = json.loads(raw)
                if (not isinstance(payload, dict) or type(payload.get("Status")) is not int
                        or payload["Status"] != 0 or payload.get("TC") is True):
                    raise PublicDNSFailure("Public DNS could not verify the requested hostname")
                questions, answers = payload.get("Question"), payload.get("Answer", [])
                if (not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict)
                        or questions[0].get("type") != record_type
                        or str(questions[0].get("name", "")).casefold().rstrip(".") != host.casefold().rstrip(".")
                        or not isinstance(answers, list)):
                    raise PublicDNSFailure("Public DNS returned an invalid verification response")
                ips = []
                for answer in answers:
                    if not isinstance(answer, dict):
                        raise PublicDNSFailure("Public DNS returned an invalid address record")
                    if answer.get("type") != record_type:
                        continue
                    if not isinstance(answer.get("data"), str):
                        raise PublicDNSFailure("Public DNS returned an invalid address record")
                    address = ipaddress.ip_address(answer["data"])
                    if address.version != version or not address.is_global:
                        raise PermissionError("Public DNS did not verify a public destination; request remains blocked")
                    ips.append(str(address))
                if ips:
                    return sorted(set(ips))
    except (PublicDNSFailure, PermissionError):
        raise
    except Exception:  # noqa: BLE001 - resolver exceptions may contain secret URLs or headers.
        # Resolver failures can contain request URLs/headers. Keep them out of
        # tool results just like destination-network errors.
        raise PublicDNSFailure("Proxy DNS returned virtual addresses; public DNS verification failed") from None
    raise PublicDNSFailure("Public DNS returned no usable public address; request remains blocked")


def request_origin(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Credential destination must be an HTTP(S) origin without userinfo")
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


def safe_url(raw: str) -> str:
    value = urlsplit(raw)
    query = urlencode([(key, "[REDACTED]" if SECRET_NAME.search(key) else val)
                       for key, val in parse_qsl(value.query, keep_blank_values=True)])
    host = value.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if value.port:
        host += f":{value.port}"
    return urlunsplit((value.scheme, host, value.path, query, ""))


async def pin_url(raw: str, allowed_hosts: set[str], *, dns_fallback="off", dns_client_factory=None) -> tuple[httpx.URL, str, str]:
    if not isinstance(dns_fallback, str) or dns_fallback not in {"off", "google"}:
        raise ValueError("http_dns_fallback must be off or google")
    url = httpx.URL(raw)
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password:
        raise ValueError("Only HTTP(S) URLs without embedded credentials are supported")
    if any(SECRET_NAME.search(key) for key, _ in parse_qsl(url.query.decode(), keep_blank_values=True)):
        raise ValueError("Credentials in URL query strings are forbidden; use credential_headers")
    addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
        url.host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM), 10)
    ips = sorted({address[4][0] for address in addresses})
    if not ips:
        raise ValueError("URL hostname did not resolve")
    if url.host.casefold() not in allowed_hosts and any(not ipaddress.ip_address(ip).is_global for ip in ips):
        try:
            ipaddress.ip_address(url.host)
            literal = True
        except ValueError:
            literal = False
        local_name = ("." not in url.host or url.host.casefold().rstrip(".").endswith(
            (".local", ".localhost", ".internal", ".home.arpa", ".test", ".invalid")))
        if (dns_fallback == "google" and not literal and not local_name
                and all(ipaddress.ip_address(ip).version == 4 and ipaddress.ip_address(ip) in FAKE_IP_NETWORK for ip in ips)):
            ips = await public_dns_addresses(url.host, client_factory=dns_client_factory)
        else:
            raise PermissionError("Private/loopback destinations require an explicit http_allowed_hosts entry")
    # Connect to the validated address, retaining Host and TLS SNI. A second DNS
    # lookup cannot turn an approved public host into a private destination.
    return url.copy_with(host=ips[0]), url.netloc.decode(), url.host


class HTMLText(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url, self.skip, self.title_depth = base_url, 0, 0
        self.text, self.titles, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
        if tag == "title":
            self.title_depth += 1
        if not self.skip and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
            self.text.append("\n")
        if tag == "a" and not self.skip and len(self.links) < 100:
            href = dict(attrs).get("href", "")
            url = urljoin(self.base_url, href)
            if urlsplit(url).scheme in {"http", "https"}:
                self.links.append(safe_url(url))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            if self.title_depth:
                self.titles.append(data)

    def result(self):
        text = "\n".join(" ".join(line.split()) for line in "".join(self.text).splitlines() if line.strip())
        return {"text": text, "title": " ".join(self.titles).strip(), "links": list(dict.fromkeys(self.links))}


class HTTPTool:
    def __init__(self, *, fetch=False, allowed_hosts=()):
        self.fetch, self.allowed_hosts = fetch, {str(host).casefold() for host in allowed_hosts}
        properties = {"url": TEXT, "timeout": {"type": "number", "minimum": 0.1, "maximum": 120},
                      "max_bytes": {"type": "integer", "minimum": 1, "maximum": 25 * 1024 * 1024},
                      "headers": {"type": "object", "propertyNames": {"not": {"pattern": SENSITIVE_HEADER_PATTERN}},
                                  "additionalProperties": {"type": "string", "pattern": "^[^\\r\\n]*$"}},
                      "credential_headers": {"type": "object", "additionalProperties": {
                          "type": "string", "pattern": "^(env|keyring):[^\\r\\n]+$"}}}
        if not fetch:
            properties.update(method={"enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
                              body={"type": "string"}, body_from_file=TEXT)
        self.spec = ToolSpec("web_fetch" if fetch else "http_request",
                             "Fetch an HTTP(S) page and extract static HTML text with source URL; no search or JavaScript" if fetch else
                             "Make a bounded HTTP request; GET/HEAD read, mutations require policy approval; credentials by reference",
                             schema(properties, ["url"]), risk="read" if fetch else "write",
                             replay_safe=fetch, concurrency_safe=fetch, timeout_seconds=135)

    def classify(self, arguments):
        read = self.fetch or arguments.get("method", "GET") in {"GET", "HEAD"}
        risk = "external" if read and arguments.get("credential_headers") else "read" if read else "write"
        return dataclasses.replace(self.spec, risk=risk, replay_safe=read,
                                   concurrency_safe=read)

    async def execute(self, arguments, context):
        dispatched, material, secrets = [False], not (self.fetch or arguments.get("method", "GET") in {"GET", "HEAD"}), []
        try:
            validate(arguments, self.spec.parameters)
            if context.cancel.is_set():
                return ToolResult("Cancelled before HTTP dispatch", True, "cancelled", "not_executed")
            task = asyncio.create_task(self._request(arguments, context, dispatched, secrets))
            stopped = asyncio.create_task(context.cancel.wait())
            try:
                done, _ = await asyncio.wait({task, stopped}, timeout=arguments.get("timeout", 30),
                                            return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    return await task
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                category = "cancelled" if context.cancel.is_set() else "timeout"
                return ToolResult(f"HTTP request {category}; no retry was attempted", True, category,
                                  "unknown" if material and dispatched[0] else "known")
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return ToolResult("HTTP request cancelled", True, "cancelled",
                                  "unknown" if material and dispatched[0] else "not_executed")
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
        except PublicDNSFailure as exc:
            return ToolResult(str(exc), True, "dns", "not_executed" if not dispatched[0] else "known")
        except (ValueError, PermissionError) as exc:
            if material and dispatched[0]:
                return ToolResult("HTTP operation ended after dispatch; remote outcome needs reconciliation", True,
                                  "http", "unknown")
            return failure(exc, outcome="not_executed" if not dispatched[0] else "known")
        except Exception as exc:  # noqa: BLE001 - network exceptions must not leak credentials.
            # Exception text can contain secret request headers/URLs. Only type
            # and bounded public state cross the tool-output/log boundary.
            return ToolResult(f"HTTP operation failed ({type(exc).__name__}); no retry attempted", True, "http",
                              "unknown" if material and dispatched[0] else "known")

    async def _request(self, args, ctx, dispatched, secrets):
        headers = dict(args.get("headers", {}))
        if any(SECRET_NAME.search(name) or name.lower() in FORBIDDEN_HEADERS for name in headers):
            raise ValueError("Sensitive/reserved headers are forbidden; use credential_headers for secrets")
        resolver = ctx.services.get("credential_resolver") or ctx.services.get("secret_resolver") or CredentialResolver()
        protected = {}
        private_refs = PRIVATE_CREDENTIALS | {str(ref).casefold() for ref in ctx.services.get("protected_credential_refs", [])}
        bindings = ctx.services.get("http_credential_origins", {})
        for name, reference in args.get("credential_headers", {}).items():
            if name.lower() in FORBIDDEN_HEADERS or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise ValueError("Invalid credential header name")
            if reference.casefold() in private_refs:
                raise PermissionError("Runtime provider credentials cannot be used by HTTP tools")
            if not reference.startswith(("env:", "keyring:")) or request_origin(args["url"]) not in {
                request_origin(origin) for origin in bindings.get(reference, [])
            }:
                raise PermissionError("Credential reference is not bound to this destination in trusted HTTP configuration")
            value = resolver(reference)
            if "\n" in value or "\r" in value:
                raise ValueError("Credential header contains a line break")
            protected[name] = value
            secrets.append(value)
        method = "GET" if self.fetch else args.get("method", "GET")
        body = args.get("body")
        if body is not None and args.get("body_from_file"):
            raise ValueError("body and body_from_file are mutually exclusive")
        if method in {"GET", "HEAD"} and (body is not None or args.get("body_from_file")):
            raise ValueError("GET/HEAD do not accept a request body")
        if args.get("body_from_file"):
            path = read_path(args["body_from_file"], ctx)
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("Request body exceeds 25 MiB")
            body = path.read_bytes()
        elif body is not None:
            body = body.encode()
            if len(body) > 25 * 1024 * 1024:
                raise ValueError("Request body exceeds 25 MiB")
        url, original = args["url"], args["url"]
        maximum = args.get("max_bytes", 5 * 1024 * 1024)
        allowed = self.allowed_hosts | {str(h).casefold() for h in ctx.services.get("http_allowed_hosts", [])}
        raw, response_headers, status = bytearray(), {}, None
        factory = ctx.services.get("http_client_factory") or (
            lambda: httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=args.get("timeout", 30)))
        async with factory() as client:
            for redirect in range(6):
                # The fixed DNS fallback receives no target headers.
                # Failed pinning aborts this hop before any destination dispatch.
                pinned, host, sni = await pin_url(url, allowed,
                    dns_fallback=ctx.services.get("http_dns_fallback", "google"),
                    dns_client_factory=ctx.services.get("http_dns_client_factory"))
                if ctx.cancel.is_set():
                    raise asyncio.CancelledError()
                request_headers = {**headers, **protected, "Host": host, "Accept-Encoding": "identity"}
                dispatched[0] = True
                async with client.stream(method, pinned, headers=request_headers, content=body,
                                         extensions={"sni_hostname": sni}, follow_redirects=False) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        if method not in {"GET", "HEAD"}:
                            return ToolResult("Mutation received a redirect; not replayed to another endpoint", True,
                                              "http_redirect", "unknown", data={"status": status, "url": safe_url(url)})
                        if redirect == 5:
                            raise ValueError("Redirect limit exceeded")
                        following = urljoin(url, response.headers["location"])
                        before, after = urlsplit(url), urlsplit(following)
                        if before.scheme == "https" and after.scheme != "https":
                            raise PermissionError("HTTPS downgrade redirects are forbidden")
                        if (before.scheme, before.hostname, before.port) != (after.scheme, after.hostname, after.port):
                            protected = {}
                        url = following
                        continue
                    size = response.headers.get("content-length")
                    if size and size.isdigit() and int(size) > maximum:
                        raise ValueError("HTTP response exceeds max_bytes")
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > maximum:
                            raise ValueError("HTTP response exceeds max_bytes")
                        raw.extend(chunk)
                    response_headers = {key: value for key, value in response.headers.items()
                                        if not SECRET_NAME.search(key) and key.lower() != "location"}
                    encoding = response.encoding or "utf-8"
                    break
        content = bytes(raw)
        for secret in secrets:
            content = content.replace(secret.encode(), b"[REDACTED]")
            response_headers = {key: value.replace(secret, "[REDACTED]") for key, value in response_headers.items()}
        mime = response_headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
        suffix = ".html" if mime == "text/html" else ".txt" if mime.startswith("text/") else ".bin"
        path = ctx.artifact_dir / f"{uuid.uuid4().hex}-response{suffix}"
        atomic_write(path, content)
        record = artifact(path, ctx, mime, kind="http-response", source_url=safe_url(url))
        data = {"requested_url": safe_url(original), "url": safe_url(url), "status": status,
                "headers": response_headers, "bytes": len(content), "mime_type": mime}
        text = content.decode(encoding, errors="replace") if mime.startswith("text/") or mime in {
            "application/json", "application/xml", "application/xhtml+xml"} else "[Binary response saved as artifact]"
        if self.fetch and mime in {"text/html", "application/xhtml+xml"}:
            parser = HTMLText(url)
            parser.feed(text)
            extracted = parser.result()
            text = extracted.pop("text")
            data.update(extracted, extraction="static-html-text; scripts/styles excluded; no JavaScript execution")
        result = bounded_result(f"Source: {safe_url(url)}\nHTTP: {status}\n\n{text}", ctx, data=data)
        result.artifacts.append(record)
        if status is not None and status >= 400:
            result.is_error, result.error_category = True, "http_status"
            if method not in {"GET", "HEAD"} and status >= 500:
                result.outcome = "unknown"
        return result
