"""Support for LG SmartThinQ device — ThinQ Connect transport."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_REGION,
    CONF_TOKEN,
    EVENT_HOMEASSISTANT_STOP,
    MAJOR_VERSION,
    MINOR_VERSION,
    Platform,
    UnitOfTemperature,
    __version__,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CLIENT,
    CONF_PAT,
    DOMAIN,
    LGE_DEVICES,
    LGE_DISCOVERY_NEW,
    MIN_HA_MAJ_VER,
    MIN_HA_MIN_VER,
    STARTUP,
    __min_ha_version__,
)
from .coordinator import ThinQCoordinator
from .wideq import (
    DeviceInfo as ThinQDeviceInfo,
    DeviceType,
    TemperatureUnit,
    get_lge_device,
)
from .wideq.thinqconnect_client import (
    ThinQAuthError,
    ThinQConnectClient,
)
from .wideq.device import Device as ThinQDevice

SMARTTHINQ_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.HUMIDIFIER,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

SIGNAL_RELOAD_ENTRY = f"{DOMAIN}_reload_entry"
DISCOVERED_DEVICES = "discovered_devices"
UNSUPPORTED_DEVICES = "unsupported_devices"
MAX_DISC_COUNT = 4

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HA version helpers (kept for backwards compat with config_flow import)
# ---------------------------------------------------------------------------


def is_min_ha_version(min_ha_major_ver: int, min_ha_minor_ver: int) -> bool:
    """Check if HA version is at least a specific version."""
    return MAJOR_VERSION > min_ha_major_ver or (
        MAJOR_VERSION == min_ha_major_ver and MINOR_VERSION >= min_ha_minor_ver
    )


def is_valid_ha_version() -> bool:
    """Check if HA version is valid for this integration."""
    return is_min_ha_version(MIN_HA_MAJ_VER, MIN_HA_MIN_VER)


# ---------------------------------------------------------------------------
# Config entry migration
# ---------------------------------------------------------------------------


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries.

    Version 1 entries used WideQ OAuth tokens which are no longer valid.
    We cannot automatically convert them to PATs, so we signal failure and
    let the user re-configure via the new flow.
    """
    _LOGGER.warning(
        "SmartThinQ config entry version %s is not supported. "
        "The WideQ OAuth transport has been removed. "
        "Please remove and re-add the integration using a ThinQ PAT.",
        entry.version,
    )
    return False


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartThinQ integration from a config entry."""

    if not is_valid_ha_version():
        _LOGGER.warning(
            "SmartThinQ requires Home Assistant %s, running %s.",
            __min_ha_version__,
            __version__,
        )
        return False

    pat = entry.data.get(CONF_PAT)
    if not pat:
        # Legacy v1 entry without a PAT — cannot proceed.
        _LOGGER.error(
            "SmartThinQ config entry has no PAT. "
            "Please remove and re-add the integration."
        )
        return False

    region = entry.data.get(CONF_REGION, "US")
    client_id: str | None = entry.data.get(CONF_CLIENT_ID)
    ha_session = async_get_clientsession(hass)

    log_info: bool = hass.data.get(DOMAIN, {}).get(SIGNAL_RELOAD_ENTRY, 0) < 2
    if log_info:
        hass.data[DOMAIN] = {SIGNAL_RELOAD_ENTRY: 2}
        _LOGGER.info(STARTUP)
        _LOGGER.info(
            "Initialising ThinQ Connect platform for region: %s", region
        )

    try:
        client = await ThinQConnectClient.from_pat(
            pat=pat,
            country=region,
            client_id=client_id,
            ha_session=ha_session,
        )
    except ThinQAuthError as exc:
        raise ConfigEntryAuthFailed(
            "ThinQ PAT rejected — please re-authenticate."
        ) from exc
    except Exception as exc:
        _LOGGER.warning("ThinQ Connect not reachable: %s", exc)
        raise ConfigEntryNotReady("ThinQ platform not ready") from exc

    if not client.has_devices:
        _LOGGER.error("No ThinQ devices found. Component setup aborted.")
        await client.close()
        return False

    _LOGGER.debug("ThinQ Connect client ready")

    # Persist client_id if we didn't have one yet.
    if not client_id and client.client_id:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CLIENT_ID: client.client_id}
        )

    try:
        lge_devices, unsupported_devices, discovered_devices = await lge_devices_setup(
            hass, client
        )
    except Exception as exc:
        _LOGGER.warning("ThinQ device setup failed: %s", exc)
        await client.close()
        raise ConfigEntryNotReady("ThinQ platform not ready") from exc

    if discovered_devices is None:
        await client.close()
        raise ConfigEntryNotReady("ThinQ platform not ready: no devices found.")

    # Remove devices that are no longer registered.
    dev_ids = [v for ids in discovered_devices.values() for v in ids]
    cleanup_orphan_lge_devices(hass, entry.entry_id, dev_ids)

    # Start MQTT listener for push updates.
    coordinators: list[ThinQCoordinator] = hass.data[DOMAIN].get("coordinators", [])
    if coordinators:
        def _on_event(device_id: str, event_data: dict) -> None:
            for coord in coordinators:
                coord.on_device_event(device_id, event_data)

        try:
            await client.async_start_mqtt_listener(_on_event)
        except Exception:
            _LOGGER.warning(
                "Could not start MQTT listener; falling back to polling only.",
                exc_info=True,
            )

    async def _close_client(event: Event) -> None:
        await client.async_stop_mqtt_listener()
        await client.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _close_client)
    )

    hass.data[DOMAIN].update(
        {
            CLIENT: client,
            LGE_DEVICES: lge_devices,
            UNSUPPORTED_DEVICES: unsupported_devices,
            DISCOVERED_DEVICES: discovered_devices,
            "coordinators": coordinators,
        }
    )

    await hass.config_entries.async_forward_entry_setups(entry, SMARTTHINQ_PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, SMARTTHINQ_PLATFORMS
    ):
        data = hass.data.pop(DOMAIN)
        reload = data.get(SIGNAL_RELOAD_ENTRY, 0)
        if reload > 0:
            hass.data[DOMAIN] = {SIGNAL_RELOAD_ENTRY: reload}
        client = data.get(CLIENT)
        if client:
            await client.async_stop_mqtt_listener()
            await client.close()
    return unload_ok


# ---------------------------------------------------------------------------
# Device wrapper
# ---------------------------------------------------------------------------


class LGEDevice:
    """Generic class that represents a LGE device."""

    def __init__(
        self,
        device: ThinQDevice,
        hass: HomeAssistant,
        root_dev_id: str | None = None,
    ) -> None:
        """Initialise a LGE Device."""
        self._device = device
        self._hass = hass
        self._root_dev_id = root_dev_id
        self._name = device.name
        self._device_id = device.unique_id
        self._type = device.device_info.type
        self._mac = None
        if mac := device.device_info.macaddress:
            self._mac = dr.format_mac(mac)
        self._firmware = device.device_info.firmware
        self._model = f"{device.device_info.model_name}"
        self._unique_id = f"{self._type.name}:{self._device_id}"
        self._state = None
        self._coordinator: ThinQCoordinator | None = None
        self._disc_count = 0
        self._available = True

    # -- properties ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def assumed_state(self) -> bool:
        return self._available and self._disc_count >= MAX_DISC_COUNT

    @property
    def device(self):
        return self._device

    @property
    def device_id(self):
        return self._device_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> DeviceType:
        return self._type

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def available_features(self) -> dict:
        return self._device.available_features

    @property
    def device_info(self) -> DeviceInfo:
        data = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._name,
            manufacturer="LG",
            model=f"{self._model} ({self._type.name})",
        )
        if self._firmware:
            data["sw_version"] = self._firmware
        if self._mac and not self._root_dev_id:
            data["connections"] = {(dr.CONNECTION_NETWORK_MAC, self._mac)}
        if self._root_dev_id:
            data["via_device"] = (DOMAIN, self._root_dev_id)
        return data

    @property
    def coordinator(self) -> ThinQCoordinator | None:
        return self._coordinator

    # -- lifecycle ----------------------------------------------------------

    async def init_device(self) -> bool:
        """Init the device status and start coordinator."""
        if not await self._device.init_device_info():
            return False
        self._state = self._device.status
        self._model = f"{self._model}-{self._device.model_info.model_type}"

        coord = ThinQCoordinator(self._hass, self._device)
        await coord.async_refresh()
        self._coordinator = coord

        # Initialize device features
        _ = self._state.device_features

        return True

    def async_set_updated(self) -> None:
        """Manually update state and notify coordinator entities."""
        if self._coordinator:
            self._coordinator.async_set_updated_data(self._state)


# ---------------------------------------------------------------------------
# Device discovery helpers
# ---------------------------------------------------------------------------


async def lge_devices_setup(
    hass: HomeAssistant,
    client: ThinQConnectClient,
    discovered_devices: dict[str, list[str]] | None = None,
) -> tuple[
    dict[DeviceType, list[LGEDevice]],
    dict[DeviceType, list[ThinQDeviceInfo]],
    dict[str, list[str]],
]:
    """Query connected devices from LG ThinQ Connect."""
    _LOGGER.debug("Searching LGE ThinQ devices...")

    wrapped_devices: dict[DeviceType, list[LGEDevice]] = {}
    unsupported_devices: dict[DeviceType, list[ThinQDeviceInfo]] = {}

    if not client.has_devices:
        await client.refresh_devices()

    if (client_devices := client.devices) is None:
        return wrapped_devices, unsupported_devices, discovered_devices

    new_devices: dict[str, list[str]] = {}
    if discovered_devices is None:
        discovered_devices = {}

    device_count = 0
    temp_unit = TemperatureUnit.CELSIUS
    if hass.config.units.temperature_unit != UnitOfTemperature.CELSIUS:
        temp_unit = TemperatureUnit.FAHRENHEIT

    async def init_device(
        lge_dev: ThinQDevice, device_info: ThinQDeviceInfo, root_dev_id: str
    ) -> bool:
        root_dev = None if root_dev_id == lge_dev.unique_id else root_dev_id
        dev = LGEDevice(lge_dev, hass, root_dev)
        if not await dev.init_device():
            _LOGGER.error(
                "Error initialising LGE Device. Name: %s - Type: %s",
                device_info.name,
                device_info.type.name,
            )
            return False

        new_devices[device_info.device_id].append(dev.device_id)
        wrapped_devices.setdefault(device_info.type, []).append(dev)

        # Register the coordinator so MQTT events can reach it.
        hass.data.setdefault(DOMAIN, {}).setdefault("coordinators", []).append(
            dev.coordinator
        )

        _LOGGER.info(
            "LGE Device added. Name: %s - Type: %s - Model: %s - ID: %s",
            dev.name,
            device_info.type.name,
            device_info.model_name,
            dev.device_id,
        )
        return True

    for device_info in client_devices:
        device_id = device_info.device_id
        if device_id in discovered_devices:
            new_devices[device_id] = discovered_devices[device_id]
            continue

        new_devices[device_id] = []
        device_count += 1

        lge_devs = get_lge_device(client, device_info, temp_unit)
        if not lge_devs:
            _LOGGER.info(
                "Found unsupported LGE Device. Name: %s - Type: %s - NetworkType: %s",
                device_info.name,
                device_info.type.name,
                device_info.network_type.name,
            )
            unsupported_devices.setdefault(device_info.type, []).append(device_info)
            continue

        root_dev = None
        for idx, lge_dev in enumerate(lge_devs):
            if idx == 0:
                root_dev = lge_dev.unique_id
            if not await init_device(lge_dev, device_info, root_dev):
                break
            if sub_dev := lge_dev.subkey_device:
                await init_device(sub_dev, device_info, root_dev)

    if device_count > 0:
        _LOGGER.info("Found %s LGE device(s)", device_count)

    return wrapped_devices, unsupported_devices, new_devices


def cleanup_orphan_lge_devices(
    hass: HomeAssistant, entry_id: str, valid_dev_ids: list[str]
) -> None:
    """Delete devices that are no longer registered in LG client app."""
    device_registry = dr.async_get(hass)
    all_lg_dev_entries = dr.async_entries_for_config_entry(device_registry, entry_id)

    valid_reg_dev_ids = []
    for device_id in valid_dev_ids:
        dev = device_registry.async_get_device({(DOMAIN, device_id)})
        if dev is not None:
            valid_reg_dev_ids.append(dev.id)

    for dev_entry in all_lg_dev_entries:
        if dev_entry.id not in valid_reg_dev_ids:
            device_registry.async_remove_device(dev_entry.id)
