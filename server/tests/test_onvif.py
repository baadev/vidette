"""ONVIF adapter tests — canned SOAP fixtures over httpx.MockTransport; no network, ever.

Fixture XML deliberately mixes namespace prefixes (SOAP-ENV/tds/trt/tt vs. env/d/wsa) the
way real vendors do, so the namespace-agnostic parsing is what's actually under test.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from vidette.adapters.base import ProbeStatus
from vidette.adapters.onvif import (
    DiscoveredDevice,
    OnvifAdapter,
    parse_probe_match,
    wsse_password_digest,
)
from vidette.core.config import CameraConfig

HOST = "203.0.113.20"

SYSTEM_DATE_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetSystemDateAndTimeResponse>
      <tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType></tds:SystemDateAndTime>
    </tds:GetSystemDateAndTimeResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

DEVICE_INFO_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <SOAP-ENV:Body>
    <tds:GetDeviceInformationResponse>
      <tds:Manufacturer>Acme</tds:Manufacturer>
      <tds:Model>Cam-9</tds:Model>
      <tds:FirmwareVersion>1.2.3</tds:FirmwareVersion>
    </tds:GetDeviceInformationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

CAPABILITIES_RESPONSE = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Media>
          <tt:XAddr>http://{HOST}:80/onvif/media</tt:XAddr>
        </tt:Media>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

TWO_PROFILES_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="prof_main" fixed="true">
        <tt:Name>MainStream</tt:Name>
        <tt:VideoEncoderConfiguration token="venc0">
          <tt:Name>encoder_main</tt:Name>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
      <trt:Profiles token="prof_sub" fixed="true">
        <tt:Name>SubStream</tt:Name>
        <tt:VideoEncoderConfiguration token="venc1">
          <tt:Name>encoder_sub</tt:Name>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

SINGLE_PROFILE_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="prof_main">
        <tt:Name>OnlyStream</tt:Name>
        <tt:VideoEncoderConfiguration token="venc0">
          <tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

STREAM_URI_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>{uri}</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

PROBE_MATCH_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:1111-aaaa</wsa:Address>
        </wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/name/FrontCam onvif://www.onvif.org/hardware/C1</d:Scopes>
        <d:XAddrs>http://203.0.113.21/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
      <d:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:2222-bbbb</wsa:Address>
        </wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/name/BackCam</d:Scopes>
        <d:XAddrs>http://203.0.113.22/onvif/device_service http://[fe80::1]/onvif</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>
"""

STREAM_URIS = {
    "prof_main": f"rtsp://{HOST}:554/main",
    "prof_sub": f"rtsp://{HOST}:554/sub",
}


def _camera(**options: object) -> CameraConfig:
    return CameraConfig.model_validate({"adapter": "onvif", "options": options})


def _default_camera() -> CameraConfig:
    return _camera(host=HOST, username="viewer", password="s3cr3t")


Handler = Callable[[httpx.Request], httpx.Response]


def _make_handler(profiles_xml: str, seen: list[str] | None = None) -> Handler:
    """Returns a MockTransport handler emulating a well-behaved ONVIF camera."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if seen is not None:
            seen.append(body)
        if "GetSystemDateAndTime" in body:
            return httpx.Response(200, text=SYSTEM_DATE_RESPONSE)
        if "GetDeviceInformation" in body:
            return httpx.Response(200, text=DEVICE_INFO_RESPONSE)
        if "GetCapabilities" in body:
            return httpx.Response(200, text=CAPABILITIES_RESPONSE)
        if "GetProfiles" in body:
            assert request.url.path == "/onvif/media", "media XAddr from GetCapabilities ignored"
            return httpx.Response(200, text=profiles_xml)
        if "GetStreamUri" in body:
            token = next(t for t in STREAM_URIS if f"<trt:ProfileToken>{t}<" in body)
            return httpx.Response(200, text=STREAM_URI_RESPONSE.format(uri=STREAM_URIS[token]))
        return httpx.Response(500, text="unexpected call")

    return handler


def _adapter(handler: Handler) -> OnvifAdapter:
    return OnvifAdapter(transport=httpx.MockTransport(handler))


# --- WS-Security digest ------------------------------------------------------------------


def test_wsse_password_digest_fixed_vector() -> None:
    # nonce = bytes(range(16)), created/password below → digest precomputed independently.
    assert (
        wsse_password_digest("AAECAwQFBgcICQoLDA0ODw==", "2026-07-09T12:00:00Z", "s3cr3t")
        == "fAJpoZxlR1FovNq+7lwZhzBfJPc="
    )


# --- stream_endpoints --------------------------------------------------------------------


async def test_stream_endpoints_two_profiles_selects_main_and_sub() -> None:
    seen: list[str] = []
    adapter = _adapter(_make_handler(TWO_PROFILES_RESPONSE, seen))
    endpoints = await adapter.stream_endpoints("front", _default_camera())
    assert [(e.role, e.url) for e in endpoints] == [
        ("main", f"rtsp://viewer:s3cr3t@{HOST}:554/main"),
        ("sub", f"rtsp://viewer:s3cr3t@{HOST}:554/sub"),
    ]
    # Authenticated calls must carry a WS-Security UsernameToken with a digest password.
    profiles_request = next(body for body in seen if "GetProfiles" in body)
    assert "UsernameToken" in profiles_request
    assert "PasswordDigest" in profiles_request


async def test_stream_endpoints_single_profile_is_main_only() -> None:
    adapter = _adapter(_make_handler(SINGLE_PROFILE_RESPONSE))
    endpoints = await adapter.stream_endpoints("front", _default_camera())
    assert len(endpoints) == 1
    assert endpoints[0].role == "main"
    assert endpoints[0].url == f"rtsp://viewer:s3cr3t@{HOST}:554/main"


async def test_stream_endpoints_respects_inject_credentials_off() -> None:
    adapter = _adapter(_make_handler(SINGLE_PROFILE_RESPONSE))
    camera = _camera(host=HOST, username="viewer", password="s3cr3t", inject_credentials=False)
    endpoints = await adapter.stream_endpoints("front", camera)
    assert endpoints[0].url == f"rtsp://{HOST}:554/main"


# --- probe -------------------------------------------------------------------------------


async def test_probe_ok_lists_profiles_with_resolutions() -> None:
    adapter = _adapter(_make_handler(TWO_PROFILES_RESPONSE))
    result = await adapter.probe("front", _default_camera())
    assert result.status is ProbeStatus.ok
    assert "MainStream (1920x1080)" in result.detail
    assert "SubStream (640x360)" in result.detail


async def test_probe_401_reports_auth_failed_with_next_step() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "GetSystemDateAndTime" in request.content.decode():
            return httpx.Response(200, text=SYSTEM_DATE_RESPONSE)
        return httpx.Response(401, text="Unauthorized")

    adapter = _adapter(handler)
    result = await adapter.probe("front", _default_camera())
    assert result.status is ProbeStatus.auth_failed
    assert "username/password" in result.detail
    assert "cameras.front.options" in result.detail


async def test_probe_connect_error_reports_unreachable_naming_the_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter(handler)
    result = await adapter.probe("front", _default_camera())
    assert result.status is ProbeStatus.unreachable
    assert HOST in result.detail
    assert "cameras.front.options" in result.detail


async def test_probe_missing_host_reports_misconfigured() -> None:
    adapter = OnvifAdapter()  # never reaches the network
    result = await adapter.probe("front", _camera(username="viewer"))
    assert result.status is ProbeStatus.misconfigured
    assert "host" in result.detail
    assert "cameras.front.options" in result.detail


async def test_probe_unknown_option_reports_misconfigured_naming_the_key() -> None:
    adapter = OnvifAdapter()
    result = await adapter.probe("front", _camera(host=HOST, pasword="typo"))
    assert result.status is ProbeStatus.misconfigured
    assert "pasword" in result.detail


# --- WS-Discovery parsing ----------------------------------------------------------------


def test_parse_probe_match_two_devices() -> None:
    devices = parse_probe_match(PROBE_MATCH_RESPONSE)
    assert devices == [
        DiscoveredDevice(
            xaddr="http://203.0.113.21/onvif/device_service",
            scopes=[
                "onvif://www.onvif.org/name/FrontCam",
                "onvif://www.onvif.org/hardware/C1",
            ],
            address="urn:uuid:1111-aaaa",
        ),
        DiscoveredDevice(
            xaddr="http://203.0.113.22/onvif/device_service",
            scopes=["onvif://www.onvif.org/name/BackCam"],
            address="urn:uuid:2222-bbbb",
        ),
    ]


def test_parse_probe_match_garbage_returns_empty_without_raising() -> None:
    assert parse_probe_match("this is not xml <<<") == []
    assert parse_probe_match("") == []
    assert parse_probe_match("<other><xml/></other>") == []
