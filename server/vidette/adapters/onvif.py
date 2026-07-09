"""ONVIF adapter — profile/stream discovery over SOAP (docs/cameras/onvif-rtsp.md).

M1 scope: probe (reachability → auth → profiles, each failure named precisely), stream
endpoint resolution (GetProfiles + GetStreamUri, highest resolution → main, lowest → sub)
and best-effort WS-Discovery of ONVIF devices on the LAN. Events and PTZ land in M2.

The SOAP 1.2 client is deliberately minimal — a handful of hand-built envelopes over
httpx, no zeep/lxml. Responses are parsed namespace-agnostically (match on local names)
because vendors disagree on prefixes far more than on structure.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from vidette.core.config import CameraConfig
from vidette.core.events import Observation

from .base import (
    AdapterError,
    AdapterInfo,
    Capability,
    ProbeResult,
    ProbeStatus,
    StreamEndpoint,
)

_log = logging.getLogger(__name__)

_DOCS = "https://github.com/baadev/vidette/blob/main/docs/cameras/onvif-rtsp.md"
_TIMEOUT_SECONDS = 5.0

_SOAP_HEADERS = {"Content-Type": "application/soap+xml; charset=utf-8"}
_PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_ENCODING_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
_ENVELOPE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema"'
    ' xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
    'oasis-200401-wss-wssecurity-secext-1.0.xsd"'
    ' xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
    'oasis-200401-wss-wssecurity-utility-1.0.xsd">'
    "{header}<s:Body>{body}</s:Body></s:Envelope>"
)

_DISCOVERY_ADDR = ("239.255.255.250", 3702)
_WS_DISCOVERY_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header>"
    "<w:MessageID>uuid:{message_id}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">'
    "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
    "</e:Header>"
    "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
    "</e:Envelope>"
)


class OnvifError(AdapterError):
    """An ONVIF SOAP call failed for a non-auth reason (fault, unexpected HTTP status)."""


class OnvifAuthError(OnvifError):
    """The device rejected both WS-Security UsernameToken and HTTP digest credentials."""


class OnvifOptions(BaseModel):
    """`cameras.<id>.options` for `adapter: onvif` — unknown keys are config errors."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 80
    username: str = ""
    password: str = ""  # users reference ${ENV}; interpolation happens before we see it
    inject_credentials: bool = True  # embed user:pass into rtsp:// URIs lacking them


# --- WS-Security -----------------------------------------------------------------------


def wsse_password_digest(nonce_b64: str, created: str, password: str) -> str:
    """PasswordDigest per WS-UsernameToken: base64(sha1(nonce + created + password))."""
    nonce = base64.b64decode(nonce_b64)
    digest = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    return base64.b64encode(digest).decode("ascii")


def _wsse_security_header(username: str, password: str) -> str:
    nonce_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = wsse_password_digest(nonce_b64, created, password)
    return (
        '<wsse:Security s:mustUnderstand="true"><wsse:UsernameToken>'
        f"<wsse:Username>{escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{_PASSWORD_DIGEST_TYPE}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_NONCE_ENCODING_TYPE}">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def _envelope(body: str, security: str = "") -> str:
    header = f"<s:Header>{security}</s:Header>" if security else ""
    return _ENVELOPE_TEMPLATE.format(header=header, body=body)


# --- Namespace-agnostic XML helpers ----------------------------------------------------


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_named(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """All descendants (and root) whose local tag name matches, any namespace."""
    return (el for el in root.iter() if _local_name(el.tag) == name)


def _first_named(root: ET.Element, name: str) -> ET.Element | None:
    return next(_iter_named(root, name), None)


def _text_of(root: ET.Element, name: str) -> str | None:
    element = _first_named(root, name)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _child_text(element: ET.Element, name: str) -> str | None:
    """Text of a *direct* child — avoids grabbing nested same-named elements."""
    for child in element:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip() or None
    return None


def _parse_xml(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


# --- Response parsing (pure) -----------------------------------------------------------


@dataclass(frozen=True)
class _Profile:
    token: str
    name: str
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def label(self) -> str:
        return f"{self.name} ({self.width}x{self.height})"


def _parse_profiles(xml_text: str) -> list[_Profile]:
    root = _parse_xml(xml_text)
    if root is None:
        return []
    profiles: list[_Profile] = []
    for element in _iter_named(root, "Profiles"):
        token = element.get("token")
        if not token:
            continue  # unusable: GetStreamUri needs the token
        name = _child_text(element, "Name") or token
        width = height = 0
        resolution = _first_named(element, "Resolution")
        if resolution is not None:
            width = int(_text_of(resolution, "Width") or 0)
            height = int(_text_of(resolution, "Height") or 0)
        profiles.append(_Profile(token=token, name=name, width=width, height=height))
    return profiles


def _parse_stream_uri(xml_text: str) -> str | None:
    root = _parse_xml(xml_text)
    return _text_of(root, "Uri") if root is not None else None


def _parse_media_xaddr(xml_text: str) -> str | None:
    root = _parse_xml(xml_text)
    if root is None:
        return None
    media = _first_named(root, "Media")
    return _text_of(media, "XAddr") if media is not None else None


def _fault_reason(xml_text: str) -> str:
    """Human-readable reason from a SOAP fault, '' when the body is not a fault."""
    root = _parse_xml(xml_text)
    if root is None or _first_named(root, "Fault") is None:
        return ""
    parts: list[str] = []
    for name in ("Value", "Text"):  # subcode value(s) + reason text
        parts.extend(text for el in _iter_named(root, name) if (text := (el.text or "").strip()))
    return "; ".join(dict.fromkeys(parts))  # dedupe, keep order


def _is_auth_fault(reason: str) -> bool:
    lowered = reason.lower()
    return "notauthorized" in lowered or "failedauthentication" in lowered or "auth" in lowered


def _inject_credentials(uri: str, username: str, password: str) -> str:
    """Embed user:pass into an RTSP URI that lacks credentials (go2rtc needs them inline)."""
    parts = urlsplit(uri)
    if not username or parts.username or not parts.hostname:
        return uri
    userinfo = quote(username, safe="")
    if password:
        userinfo += f":{quote(password, safe='')}"
    host = parts.hostname
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


def _format_validation_error(exc: ValidationError) -> str:
    issues = [
        f"{'.'.join(str(part) for part in error['loc']) or 'options'}: {error['msg']}"
        for error in exc.errors()
    ]
    return "; ".join(issues)


# --- SOAP client -----------------------------------------------------------------------


async def _soap_call(
    client: httpx.AsyncClient,
    url: str,
    body: str,
    options: OnvifOptions,
    call_name: str,
) -> str:
    """POST one SOAP 1.2 envelope. WS-Security first; one retry with HTTP digest on 401."""
    security = _wsse_security_header(options.username, options.password) if options.username else ""
    response = await client.post(url, content=_envelope(body, security), headers=_SOAP_HEADERS)
    if response.status_code == 401 and options.username:
        # Some devices ignore WSSE and want RFC 7616 HTTP digest instead — try once.
        response = await client.post(
            url,
            content=_envelope(body),
            headers=_SOAP_HEADERS,
            auth=httpx.DigestAuth(options.username, options.password),
        )
    if response.status_code == 401:
        raise OnvifAuthError(f"{call_name}: device returned HTTP 401 to WSSE and HTTP digest auth")
    if response.is_success:
        return response.text
    reason = _fault_reason(response.text)
    if reason and _is_auth_fault(reason):
        raise OnvifAuthError(f"{call_name}: {reason}")
    detail = f" — {reason}" if reason else ""
    raise OnvifError(f"{call_name}: HTTP {response.status_code}{detail}")


class OnvifAdapter:
    info = AdapterInfo(
        id="onvif",
        display_name="ONVIF",
        maturity="beta",  # probe + streams work; events/PTZ land in M2
        capabilities=Capability.LIVE_MAIN | Capability.LIVE_SUB | Capability.SNAPSHOT,
        docs_url=_DOCS,
    )

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` is a keyword-only test seam (httpx.MockTransport); never set in prod."""
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport)

    @staticmethod
    def _device_url(options: OnvifOptions) -> str:
        return f"http://{options.host}:{options.port}/onvif/device_service"

    async def _media_url(
        self, client: httpx.AsyncClient, device_url: str, options: OnvifOptions
    ) -> str:
        """Media service XAddr via GetCapabilities; conventional path when discovery fails."""
        fallback = f"http://{options.host}:{options.port}/onvif/media_service"
        body = "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>"
        try:
            response_text = await _soap_call(client, device_url, body, options, "GetCapabilities")
        except OnvifAuthError:
            raise
        except (OnvifError, httpx.HTTPError):
            return fallback
        return _parse_media_xaddr(response_text) or fallback

    async def _profiles(
        self, client: httpx.AsyncClient, media_url: str, options: OnvifOptions
    ) -> list[_Profile]:
        response_text = await _soap_call(
            client, media_url, "<trt:GetProfiles/>", options, "GetProfiles"
        )
        return _parse_profiles(response_text)

    async def _stream_uri(
        self, client: httpx.AsyncClient, media_url: str, options: OnvifOptions, token: str
    ) -> str | None:
        body = (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        response_text = await _soap_call(client, media_url, body, options, "GetStreamUri")
        return _parse_stream_uri(response_text)

    async def probe(self, camera_id: str, config: CameraConfig) -> ProbeResult:
        try:
            options = OnvifOptions.model_validate(config.options)
        except ValidationError as exc:
            return ProbeResult(
                ProbeStatus.misconfigured,
                f"camera '{camera_id}': invalid ONVIF options "
                f"({_format_validation_error(exc)}) — fix 'cameras.{camera_id}.options', "
                f"see {_DOCS}",
            )
        device_url = self._device_url(options)
        async with self._client() as client:
            # 1. Reachability: GetSystemDateAndTime needs no auth; any HTTP answer counts.
            try:
                await client.post(
                    device_url,
                    content=_envelope("<tds:GetSystemDateAndTime/>"),
                    headers=_SOAP_HEADERS,
                )
            except httpx.HTTPError as exc:
                return ProbeResult(
                    ProbeStatus.unreachable,
                    f"camera '{camera_id}': no ONVIF response from "
                    f"{options.host}:{options.port} ({exc.__class__.__name__}) — check "
                    f"host/port in 'cameras.{camera_id}.options' and that ONVIF is enabled "
                    "in the camera's settings",
                )
            # 2. Auth + capabilities + profiles.
            try:
                device_xml = await _soap_call(
                    client,
                    device_url,
                    "<tds:GetDeviceInformation/>",
                    options,
                    "GetDeviceInformation",
                )
                media_url = await self._media_url(client, device_url, options)
                profiles = await self._profiles(client, media_url, options)
            except OnvifAuthError:
                return ProbeResult(
                    ProbeStatus.auth_failed,
                    f"camera '{camera_id}': the device rejected the ONVIF credentials — "
                    f"check username/password in cameras.{camera_id}.options",
                )
            except httpx.HTTPError as exc:
                return ProbeResult(
                    ProbeStatus.unreachable,
                    f"camera '{camera_id}': ONVIF service at {options.host}:{options.port} "
                    f"stopped answering mid-probe ({exc.__class__.__name__}) — check the "
                    "network path and the camera's ONVIF service",
                )
            except OnvifError as exc:
                return ProbeResult(
                    ProbeStatus.misconfigured,
                    f"camera '{camera_id}': device answered but the ONVIF call failed "
                    f"({exc}) — the camera may not support ONVIF Profile S; see {_DOCS}",
                )
        if not profiles:
            return ProbeResult(
                ProbeStatus.misconfigured,
                f"camera '{camera_id}': the device reported no media profiles — enable at "
                "least one stream profile in the camera's web UI",
            )
        device_root = _parse_xml(device_xml)
        model = ""
        if device_root is not None:
            manufacturer = _text_of(device_root, "Manufacturer") or ""
            model = f"{manufacturer} {_text_of(device_root, 'Model') or ''}".strip()
        described = ", ".join(profile.label for profile in profiles)
        prefix = f"{model}: " if model else ""
        return ProbeResult(
            ProbeStatus.ok,
            f"{prefix}ONVIF reachable and authenticated; {len(profiles)} profile(s): {described}",
        )

    async def stream_endpoints(self, camera_id: str, config: CameraConfig) -> list[StreamEndpoint]:
        try:
            options = OnvifOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(
                f"camera '{camera_id}': invalid ONVIF options "
                f"({_format_validation_error(exc)}) — fix 'cameras.{camera_id}.options', "
                f"see {_DOCS}"
            ) from exc
        try:
            async with self._client() as client:
                device_url = self._device_url(options)
                media_url = await self._media_url(client, device_url, options)
                profiles = await self._profiles(client, media_url, options)
                if not profiles:
                    raise AdapterError(
                        f"camera '{camera_id}': the device reported no media profiles — "
                        "enable at least one stream profile in the camera's web UI"
                    )
                # Highest resolution serves recording, lowest serves analysis/preview.
                ordered = sorted(profiles, key=lambda p: p.pixels, reverse=True)
                chosen: list[tuple[Literal["main", "sub"], _Profile]] = [("main", ordered[0])]
                if len(ordered) > 1:
                    chosen.append(("sub", ordered[-1]))
                endpoints: list[StreamEndpoint] = []
                for role, profile in chosen:
                    uri = await self._stream_uri(client, media_url, options, profile.token)
                    if not uri:
                        raise AdapterError(
                            f"camera '{camera_id}': GetStreamUri returned no URI for "
                            f"profile '{profile.name}' — the profile may be misconfigured "
                            "on the camera"
                        )
                    if options.inject_credentials:
                        uri = _inject_credentials(uri, options.username, options.password)
                    endpoints.append(StreamEndpoint(role=role, url=uri))
        except OnvifAuthError as exc:
            raise AdapterError(
                f"camera '{camera_id}': the device rejected the ONVIF credentials ({exc}) "
                f"— check username/password in cameras.{camera_id}.options"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"camera '{camera_id}': cannot reach the ONVIF service at "
                f"{options.host}:{options.port} ({exc.__class__.__name__}) — check "
                f"host/port in 'cameras.{camera_id}.options'"
            ) from exc
        return endpoints

    async def observations(
        self, camera_id: str, config: CameraConfig
    ) -> AsyncIterator[Observation]:
        """ONVIF event subscriptions (motion, tamper, IO) land in M2; nothing is pushed yet."""
        nothing: tuple[Observation, ...] = ()
        for observation in nothing:  # empty async generator, typed and mypy-clean
            yield observation


# --- WS-Discovery ----------------------------------------------------------------------


@dataclass
class DiscoveredDevice:
    xaddr: str  # first device-service URL the camera advertised
    scopes: list[str]  # onvif://www.onvif.org/... scope URIs (name, hardware, location)
    address: str  # WS-Addressing endpoint reference (urn:uuid:...)


def parse_probe_match(xml_text: str) -> list[DiscoveredDevice]:
    """ProbeMatches envelope → devices; malformed or unrelated input yields [], never raises."""
    root = _parse_xml(xml_text)
    if root is None:
        return []
    devices: list[DiscoveredDevice] = []
    for match in _iter_named(root, "ProbeMatch"):
        xaddrs = (_text_of(match, "XAddrs") or "").split()
        if not xaddrs:
            continue  # nothing to connect to; skip rather than invent
        devices.append(
            DiscoveredDevice(
                xaddr=xaddrs[0],
                scopes=(_text_of(match, "Scopes") or "").split(),
                address=_text_of(match, "Address") or "",
            )
        )
    return devices


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        for device in parse_probe_match(data.decode("utf-8", errors="replace")):
            self.devices.setdefault(device.xaddr, device)


async def discover(timeout_s: float = 3.0) -> list[DiscoveredDevice]:
    """Best-effort WS-Discovery probe for ONVIF cameras on the local network.

    Multicasts a Probe for dn:NetworkVideoTransmitter and collects ProbeMatches until the
    timeout. Network problems (no route, multicast blocked, firewalled) are logged and
    yield an empty list — discovery failing must never break configured cameras.
    """
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _DiscoveryProtocol,
            family=socket.AF_INET,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    except OSError as exc:
        _log.warning("ONVIF WS-Discovery unavailable (cannot open UDP socket): %s", exc)
        return []
    try:
        message = _WS_DISCOVERY_PROBE.format(message_id=uuid.uuid4())
        transport.sendto(message.encode(), _DISCOVERY_ADDR)
        await asyncio.sleep(timeout_s)
    except OSError as exc:
        _log.warning("ONVIF WS-Discovery probe failed: %s", exc)
        return []
    finally:
        transport.close()
    return list(protocol.devices.values())
