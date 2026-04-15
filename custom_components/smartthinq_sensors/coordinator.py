"""DataUpdateCoordinator for SmartThinQ — ThinQ Connect transport."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .wideq.device import Device as ThinQDevice

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)


class ThinQCoordinator(DataUpdateCoordinator):
    """Coordinator that combines 60-second polling with MQTT push updates.

    The coordinator owns a single ThinQDevice.  When the MQTT listener
    fires ``on_device_event`` we merge the delta into the device's
    snapshot and call ``async_set_updated_data`` so all subscribed
    entities are notified immediately — no waiting for the next poll.
    """

    def __init__(self, hass: HomeAssistant, device: ThinQDevice) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"smartthinq_sensors-{device.name}",
            update_method=self._async_poll,
            update_interval=POLL_INTERVAL,
        )
        self._device = device
        self._mqtt_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Polling path (fallback / heartbeat)
    # ------------------------------------------------------------------

    async def _async_poll(self):
        """Fetch fresh state from the ThinQ Connect REST API."""
        try:
            state = await self._device.poll()
        except Exception as exc:
            raise UpdateFailed(f"ThinQ poll failed for {self._device.name}: {exc}") from exc

        if state is not None:
            _LOGGER.debug("ThinQ poll updated state for %s", self._device.name)
            return state

        # poll() returning None means the device is off / no new data;
        # keep whatever the last known state was.
        return self.data

    # ------------------------------------------------------------------
    # Push path (MQTT)
    # ------------------------------------------------------------------

    @callback
    def on_device_event(self, device_id: str, event_data: dict) -> None:
        """Handle a push notification from ThinQ Connect MQTT.

        ``event_data`` is the raw payload dict from the MQTT message.
        We merge it into the device's snapshot and notify subscribers.
        """
        if device_id != self._device.unique_id:
            return

        _LOGGER.debug(
            "MQTT push for %s: %s",
            self._device.name,
            list(event_data.keys()),
        )

        # Merge the delta into the device's cached snapshot so that
        # subsequent polls and entity reads see consistent data.
        try:
            self._device.set_device_snapshot(event_data, device_id)
        except Exception:
            _LOGGER.warning(
                "Failed to merge MQTT payload for %s",
                self._device.name,
                exc_info=True,
            )
            return

        # Notify all subscribed entities immediately.
        self.async_set_updated_data(self._device.status)
