"""ThinQ Connect transport client.

Replaces core_async.py / core_v2.py with the official LG ThinQ Connect REST API
and PAT-based authentication.  Provides a session shim that preserves the interface
expected by the existing wideq device layer so device files remain untouched.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable

from aiohttp import ClientSession, ClientResponseError

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------

_COUNTRY_TO_REGION: dict[str, str] = {
    "US": "US",
    "CA": "US",
    "MX": "US",
    "BR": "US",
    "GB": "EU",
    "DE": "EU",
    "FR": "EU",
    "IT": "EU",
    "ES": "EU",
    "NL": "EU",
    "PL": "EU",
    "SE": "EU",
    "NO": "EU",
    "DK": "EU",
    "FI": "EU",
    "CH": "EU",
    "AT": "EU",
    "BE": "EU",
    "PT": "EU",
    "KR": "KR",
    "AU": "AU",
    "NZ": "AU",
}

_THINQ_CONNECT_BASE = "https://api-{region}.lgthinq.com"
_THINQ2_BASE = "https://thinq2-{region}.lgeapis.com/v2"

# ThinQ Connect API key (public, from SDK)
_API_KEY = "v1XTEIVNSMFRnj7jI_J1TtGDmEY1njj7L8nePjKmGm8"


def _region_for_country(country: str) -> str:
    """Return the ThinQ Connect region code for a given country code."""
    return _COUNTRY_TO_REGION.get(country.upper(), "US")


# ---------------------------------------------------------------------------
# Device-type mapping  (ThinQ Connect string → DeviceType int)
# ---------------------------------------------------------------------------

DEVICE_TYPE_MAP: dict[str, int] = {
    "REFRIGERATOR": 101,
    "KIMCHI_REFRIGERATOR": 102,
    "WATER_PURIFIER": 103,
    "WASHER": 201,
    "DRYER": 202,
    "STYLER": 203,
    "DISH_WASHER": 204,
    "WASHTOWER_WASHER": 221,
    "WASHTOWER_DRYER": 222,
    "WASHTOWER": 223,
    "OVEN": 301,
    "MICROWAVE_OVEN": 302,
    "COOKTOP": 303,
    "HOOD": 304,
    "AIR_CONDITIONER": 401,
    "AIR_PURIFIER": 402,
    "DEHUMIDIFIER": 403,
    "CEILING_FAN": 405,
    "WATER_HEATER": 406,
    "AIR_PURIFIER_FAN": 410,
    "ROBOT_CLEANER": 501,
    "STICK_CLEANER": 504,
}

# Minimal model JSON that satisfies ModelInfoV2.is_valid_model_data().
# All capability lookups return empty/None; energy sensors bypass these entirely.
_MINIMAL_MODEL_INFO: dict[str, Any] = {
    "MonitoringValue": {},
    "Info": {"modelType": ""},
    "Config": {},
    "ControlWifi": {},
}

# Sentinel prefix stored in DeviceInfo.model_info_url to trigger interception.
_TC_PROFILE_SCHEME = "thinqconnect-profile://"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ThinQAuthError(Exception):
    """Raised on 401/403 responses — PAT is missing, expired, or revoked."""


class ThinQRateLimitError(Exception):
    """Raised when the API returns error code 9012 (quota / rate limit)."""


class ThinQAPIError(Exception):
    """Raised for any other non-2xx API response."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(f"ThinQ API error {code}: {message}")
        self.code = code


# ---------------------------------------------------------------------------
# Session shim — wraps ThinQ Connect REST API, mirrors the ClientAsync.session
# interface expected by wideq/device.py and wideq/devices/*.py
# ---------------------------------------------------------------------------


class ThinQConnectSession:
    """Compatibility session over the official ThinQ Connect REST API."""

    def __init__(
        self,
        pat: str,
        country: str,
        client_id: str,
        ha_session: ClientSession | None = None,
    ) -> None:
        self._pat = pat
        self._country = country
        self._client_id = client_id
        self._region = _region_for_country(country)
        self._base = _THINQ_CONNECT_BASE.format(region=self._region)
        self._owned_session = ha_session is None
        self._ha_session = ha_session
        self._session: ClientSession | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> ClientSession:
        """Return (or lazily create) the underlying aiohttp session."""
        if self._ha_session is not None:
            return self._ha_session
        if self._session is None or self._session.closed:
            self._session = ClientSession()
        return self._session

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build common request headers."""
        headers = {
            "Authorization": f"Bearer {self._pat}",
            "x-country": self._country,
            "x-message-id": str(uuid.uuid4()).replace("-", "")[:22],
            "x-client-id": self._client_id,
            "x-api-key": _API_KEY,
            "x-service-phase": "OP",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Any:
        """Execute an authenticated request against the ThinQ Connect API."""
        url = f"{self._base}/{endpoint}"
        session = self._get_session()
        try:
            async with session.request(
                method, url, headers=self._headers(), **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise ThinQAuthError(
                        f"Authentication failed ({resp.status}) for {url}"
                    )
                payload = await resp.json(content_type=None)
                if not resp.ok:
                    code = str(payload.get("resultCode", "")) if isinstance(payload, dict) else ""
                    msg = str(payload.get("resultMsg", resp.reason)) if isinstance(payload, dict) else resp.reason
                    if code == "9012":
                        raise ThinQRateLimitError(f"Rate limit / quota exceeded: {msg}")
                    raise ThinQAPIError(code, msg)
                return (payload or {}).get("response", payload)
        except (ThinQAuthError, ThinQRateLimitError, ThinQAPIError):
            raise
        except Exception as exc:
            raise ThinQAPIError(None, str(exc)) from exc

    async def close(self) -> None:
        """Close the owned session if we created it."""
        if self._owned_session and self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Methods expected by wideq/device.py  (Monitor / Device)
    # ------------------------------------------------------------------

    async def get2(self, path: str) -> dict:
        """Generic GET against the ThinQ Connect API.

        Legacy callers pass ThinQ2-style paths such as
        ``service/devices/{id}``.  We map those to ThinQ Connect equivalents
        where possible and return an empty dict for anything else.
        """
        # Map the well-known status path.
        if path.startswith("service/devices/") and not path.endswith("/"):
            parts = path.split("/")
            if len(parts) >= 3:
                device_id = parts[2]
                return await self.get_device_v2_settings(device_id)

        # Energy history path is handled separately; fall through to empty.
        _LOGGER.debug("Unmapped get2 path (returning {}): %s", path)
        return {}

    async def get_device_v2_settings(self, device_id: str) -> dict:
        """Return the current device state from ThinQ Connect.

        The response uses ThinQ Connect's resource-based schema which differs
        from ThinQ2's ``washerDryer`` snapshot format.  With a minimal
        ``MonitoringValue`` in the model JSON, decode_snapshot will simply
        return empty dicts and devices will show as unavailable until Phase 3
        adds schema translation.  Energy sensors bypass this entirely.
        """
        try:
            return await self._request("GET", f"devices/{device_id}/state") or {}
        except (ThinQAuthError, ThinQRateLimitError):
            raise
        except Exception as exc:
            _LOGGER.debug("get_device_v2_settings(%s) failed: %s", device_id, exc)
            return {}

    async def device_v2_controls(
        self,
        device_id: str,
        ctrl_key: Any,
        command: str | None = None,
        key: str | None = None,
        value: Any = None,
        *,
        ctrl_path: str | None = None,
    ) -> dict:
        """Send a control command via ThinQ Connect."""
        payload: dict[str, Any]
        if isinstance(ctrl_key, dict):
            payload = ctrl_key
        else:
            payload = {
                "ctrlKey": ctrl_key,
                "command": command or "",
                "dataKey": key or "",
                "dataValue": value or "",
            }
        try:
            return await self._request(
                "POST", f"devices/{device_id}/control", json=payload
            ) or {}
        except (ThinQAuthError, ThinQRateLimitError):
            raise
        except Exception as exc:
            _LOGGER.debug("device_v2_controls(%s) failed: %s", device_id, exc)
            return {}

    async def post2(self, path: str, data: dict) -> dict:
        """Generic POST helper used by some device files."""
        if path.startswith("service/devices/") and "control" in path:
            parts = path.split("/")
            if len(parts) >= 3:
                device_id = parts[2]
                return await self.device_v2_controls(device_id, data)
        _LOGGER.debug("Unmapped post2 path: %s", path)
        return {}

    async def get_energy_history(
        self,
        device_id: str,
        period: str = "day",
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        """Fetch laundry energy history via ThinQ Connect.

        The ThinQ Connect API exposes energy data under
        ``/devices/{id}/energy-history`` with the same query parameters as
        the legacy ThinQ2 endpoint.
        """
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        endpoint = (
            f"devices/{device_id}/energy-history"
            f"?type=period&period={period}"
            f"&startDate={start_date}&endDate={end_date}"
        )
        try:
            return await self._request("GET", endpoint) or {}
        except Exception as exc:
            _LOGGER.debug(
                "get_energy_history(%s) failed: %s — returning empty", device_id, exc
            )
            return {}


# ---------------------------------------------------------------------------
# Main client — replaces ClientAsync
# ---------------------------------------------------------------------------


class ThinQConnectClient:
    """Official ThinQ Connect transport client.

    Implements the interface expected by __init__.py and the wideq device
    layer so that all existing device files continue to work without changes.
    """

    def __init__(
        self,
        pat: str,
        country: str,
        client_id: str,
        ha_session: ClientSession | None = None,
    ) -> None:
        self._pat = pat
        self._country = country
        self._client_id = client_id
        self._region = _region_for_country(country)
        self._base = _THINQ_CONNECT_BASE.format(region=self._region)
        self._session = ThinQConnectSession(pat, country, client_id, ha_session)
        self._devices: list[Any] | None = None
        self._model_url_cache: dict[str, dict] = {}
        self._mqtt_task = None
        self._mqtt_client = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def from_pat(
        cls,
        pat: str,
        country: str,
        client_id: str | None = None,
        ha_session: ClientSession | None = None,
    ) -> "ThinQConnectClient":
        """Create and validate a client using a Personal Access Token."""
        cid = client_id or str(uuid.uuid4())
        client = cls(pat, country, cid, ha_session)
        await client.refresh_devices()
        return client

    # ------------------------------------------------------------------
    # Properties expected by __init__.py / device.py
    # ------------------------------------------------------------------

    @property
    def has_devices(self) -> bool:
        """Return True if at least one device has been discovered."""
        return bool(self._devices)

    @property
    def devices(self):
        """Return the list of DeviceInfo objects."""
        return self._devices or []

    @property
    def client_id(self) -> str:
        """Return the client UUID used in API headers."""
        return self._client_id

    @property
    def session(self) -> ThinQConnectSession:
        """Return the session shim used by device classes."""
        return self._session

    @property
    def emulation(self) -> bool:
        """Always False — no emulation mode in ThinQ Connect client."""
        return False

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def refresh_devices(self) -> None:
        """Fetch the device list from ThinQ Connect and build DeviceInfo objects."""
        from .device_info import DeviceInfo  # local import to avoid circular

        raw_devices = await self._session._request("GET", "devices") or []
        if not isinstance(raw_devices, list):
            raw_devices = []

        discovered = []
        for dev in raw_devices:
            tc_type = dev.get("deviceType", "")
            type_id = DEVICE_TYPE_MAP.get(tc_type)
            if type_id is None:
                _LOGGER.debug(
                    "Skipping unsupported ThinQ Connect device type: %s", tc_type
                )
                continue

            device_id = dev.get("deviceId", "")
            normalized: dict[str, Any] = {
                "deviceId": device_id,
                "alias": dev.get("alias") or dev.get("deviceId", ""),
                "modelName": dev.get("modelName", ""),
                "deviceType": type_id,
                # thinq2 platform keeps _should_poll = False in Device base class
                "platformType": "thinq2",
                # WIFI networkType string
                "networkType": "02",
                "online": dev.get("online", False),
                "snapshot": {},
                # Sentinel URL — intercepted by model_url_info() below
                "modelJsonUri": f"{_TC_PROFILE_SCHEME}{device_id}",
            }
            discovered.append(DeviceInfo(normalized))

        self._devices = discovered
        _LOGGER.debug("ThinQ Connect: discovered %d supported device(s)", len(discovered))

    # ------------------------------------------------------------------
    # Model info — intercepted to avoid external download failure
    # ------------------------------------------------------------------

    async def model_url_info(self, url: str | None, device: Any = None) -> dict | None:
        """Return model data for a device.

        For the sentinel ``thinqconnect-profile://`` URLs we inject during
        device discovery, we return a minimal but valid ModelInfoV2 payload so
        devices initialize.  All real capability lookups will return None/empty;
        energy sensors bypass capability lookups and still work correctly.
        """
        if not url:
            # Matches ClientAsync behaviour: empty URL → empty dict (not None)
            return {}

        if url in self._model_url_cache:
            return self._model_url_cache[url]

        if url.startswith(_TC_PROFILE_SCHEME):
            # Return minimal valid ModelInfoV2 payload; no external request needed.
            self._model_url_cache[url] = _MINIMAL_MODEL_INFO
            return _MINIMAL_MODEL_INFO

        # For any other URL (e.g., old CDN model JSON), attempt a plain download.
        try:
            session = self._session._get_session()
            async with session.get(url) as resp:
                if resp.ok:
                    data = await resp.json(content_type=None)
                    self._model_url_cache[url] = data
                    return data
        except Exception as exc:
            _LOGGER.debug("Failed to download model info from %s: %s", url, exc)

        return None

    # ------------------------------------------------------------------
    # MQTT push listener
    # ------------------------------------------------------------------

    async def async_start_mqtt_listener(
        self, on_event: Callable[[str, dict], None]
    ) -> None:
        """Start the AWS IoT MQTT listener for real-time push events.

        Falls back silently to polling-only mode if MQTT setup fails (e.g.,
        missing awscrt native library in the HA container).
        """
        try:
            from thinqconnect.mqtt_client import ThinQMQTTClient
            from thinqconnect.thinq_api import ThinQApi

            session = self._session._get_session()
            thinq_api = ThinQApi(
                session=session,
                access_token=self._pat,
                country_code=self._country,
                client_id=self._client_id,
            )

            def _on_message(topic: str, payload: bytes, dup: bool, qos: Any, retain: bool, **_: Any) -> None:
                import json
                try:
                    data = json.loads(payload)
                    device_id = data.get("deviceId") or data.get("device_id", "")
                    push_data = data.get("report") or data.get("push") or data
                    on_event(device_id, push_data)
                except Exception as exc:
                    _LOGGER.debug("MQTT message parse error: %s", exc)

            self._mqtt_client = ThinQMQTTClient(
                thinq_api=thinq_api,
                client_id=self._client_id,
                on_message_received=_on_message,
            )
            await self._mqtt_client.async_init()
            await self._mqtt_client.async_connect()
            _LOGGER.debug("ThinQ Connect MQTT listener started")
        except Exception as exc:
            _LOGGER.warning(
                "MQTT listener could not start (polling fallback active): %s", exc
            )
            self._mqtt_client = None

    async def async_stop_mqtt_listener(self) -> None:
        """Disconnect the MQTT client if one was started."""
        if self._mqtt_client is not None:
            try:
                await self._mqtt_client.async_disconnect()
            except Exception as exc:
                _LOGGER.debug("Error disconnecting MQTT client: %s", exc)
            self._mqtt_client = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release resources."""
        await self.async_stop_mqtt_listener()
        await self._session.close()
