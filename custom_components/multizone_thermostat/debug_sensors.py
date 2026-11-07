"""Debug-only sensors exposing the latest telemetry records as entities (off by default).

They read the in-memory `latest` view of the process-wide telemetry recorder, so they cost nothing
unless enabled (Options -> Diagnostics -> debug sensors) and never influence control. Values are
published as attributes that are excluded from the HA recorder database to avoid bloating history.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

try:
    from homeassistant.const import MATCH_ALL
except ImportError:  # pragma: no cover - very old/stub HA
    MATCH_ALL = "*"

from . import telemetry
from .const import DOMAIN, make_via_device_kwargs

_LOGGER = logging.getLogger(__name__)

REFRESH_SECONDS = 10

# (kind, label, state field, key by: "climate" entity id or "zone" name)
ZONE_DEBUG_SENSORS = (
    (telemetry.KIND_PID, "PID Debug", "demand", "climate"),
    (telemetry.KIND_ESTIMATOR, "Estimator Debug", "heating_rate", "climate"),
    (telemetry.KIND_PWM, "Valve PWM Debug", "state", "zone"),
)
BOILER_DEBUG_SENSOR = (telemetry.KIND_BOILER, "Boiler Debug", "reason", "boiler")


def _scalar(value: Any) -> Any:
    """Make a value safe as an HA state/attribute: no NaN/inf, no exotic types."""
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value]
    return repr(value)


class TelemetryDebugSensor(SensorEntity):
    """Latest telemetry record of one (zone, kind), shown as state + attributes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bug-outline"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, hass: HomeAssistant, entry_id: str, coordinator, label: str, record_zone: str,
                 kind: str, state_field: str) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._label = label
        self._record_zone = record_zone
        self._kind = kind
        self._state_field = state_field
        self._last_seq: int | None = None
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_debug_{kind}_{record_zone}"
        master_dev_id = getattr(coordinator, "master_device_id", None)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_virtual_thermostats")},
            name="Multizone Brain",
            manufacturer="Custom Integration",
            model="Multizone Brain",
            **make_via_device_kwargs(entry_id, master_dev_id),
        )

    @property
    def name(self) -> str:
        return self._label

    def _record(self) -> dict[str, Any] | None:
        return telemetry.get_recorder().latest(self._record_zone, self._kind)

    @property
    def native_value(self) -> Any:
        rec = self._record()
        if rec is None:
            return None
        # HA cannot represent NaN/inf as a state, so _scalar turns them into the strings "nan"/"inf"
        return _scalar(rec["data"].get(self._state_field))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self._record()
        if rec is None:
            return {"telemetry": "no record yet"}
        attrs = {k: _scalar(v) for k, v in rec["data"].items()}
        attrs.update(record_seq=rec["seq"], record_wall=rec["wall"], record_mono=rec["mono"],
                     record_kind=rec["kind"], record_zone=rec["zone"])
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, timedelta(seconds=REFRESH_SECONDS))
        )

    async def _async_refresh(self, _now=None) -> None:
        rec = self._record()
        seq = rec["seq"] if rec else None
        if seq != self._last_seq:  # only write when something new was recorded
            self._last_seq = seq
            self.async_write_ha_state()


def build_debug_sensors(hass: HomeAssistant, entry_id: str, coordinator, zones: list[tuple[str, str]]
                        ) -> list[TelemetryDebugSensor]:
    """`zones` is a list of (zone display name, climate entity id)."""
    sensors: list[TelemetryDebugSensor] = []
    for name, climate_entity_id in zones:
        for kind, label, field, keyed_by in ZONE_DEBUG_SENSORS:
            record_zone = climate_entity_id if keyed_by == "climate" else name
            sensors.append(TelemetryDebugSensor(hass, entry_id, coordinator, f"{name} {label}",
                                                record_zone, kind, field))
    kind, label, field, record_zone = BOILER_DEBUG_SENSOR
    sensors.append(TelemetryDebugSensor(hass, entry_id, coordinator, label, record_zone, kind, field))
    return sensors
