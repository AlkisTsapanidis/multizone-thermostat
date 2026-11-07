"""Frost protection safety module."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from .. import telemetry
from ..const import (
    CONF_ZONE_CLIMATES,
    CONF_ZONE_NAME,
    DEFAULT_ANTI_FROST_ENABLED,
    DEFAULT_FROST_PROTECTION_TEMP,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    KEY_ANTI_FROST_ENABLED,
    KEY_FROST_PROTECTION_TEMP,
    make_zone_entity_id,
)

SERVICE_SET_TEMPERATURE = "set_temperature"

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class FrostManager:
    """Manages anti-frost protection checks and emergency actions."""

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        """Initialize frost manager."""
        self.hass = hass
        self.coordinator = coordinator

    def is_zone_in_frost_emergency(self, climate_id: str) -> bool:
        """Return True if anti-frost is enabled and zone temperature is below the safety threshold."""
        if not self.coordinator.get_persistent_data(KEY_ANTI_FROST_ENABLED, DEFAULT_ANTI_FROST_ENABLED):
            telemetry.get_recorder().record(telemetry.KIND_EVENT, climate_id, event="frost_check", enabled=False)
            return False

        # Try virtual thermostat temperature first
        st = self.hass.states.get(climate_id)
        current_temp = None
        source = None
        trv_samples = []
        is_faulty = (
            self.coordinator.plant_diagnostics.has_critical_sensor_fault(climate_id)
            if hasattr(self.coordinator, "plant_diagnostics")
            else False
        )
        if st and not is_faulty:
            current_temp = st.attributes.get("current_temperature")
            if current_temp is not None:
                source = "zone"

        # WARN-02: Fallback — average TRV sensor readings if virtual thermostat has no temperature yet
        # (e.g., right after boot before TRVs have reported back)
        if current_temp is None:
            zone = self.coordinator.zone_manager.get_zone(climate_id)
            if zone:
                trv_temps = []
                for trv in (zone.get(CONF_ZONE_CLIMATES) or zone.get("climate_entities") or []):
                    trv_st = self.hass.states.get(trv)
                    if trv_st:
                        t = trv_st.attributes.get("current_temperature")
                        trv_samples.append({"entity": trv, "temperature": t})
                        if t is not None:
                            try:
                                trv_temps.append(float(t))
                            except (ValueError, TypeError):
                                pass
                    else:
                        trv_samples.append({"entity": trv, "temperature": None})
                if trv_temps:
                    current_temp = sum(trv_temps) / len(trv_temps)
                    source = "trv_average"
                    _LOGGER.debug(
                        "Frost check for %s: using avg TRV temp %.1f°C (virtual has no reading)",
                        climate_id,
                        current_temp,
                    )

        if current_temp is None:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, climate_id, event="frost_check", enabled=True,
                temperature=None, source=source, trv_samples=trv_samples, sensor_fault=is_faulty, result=False,
            )
            return False

        frost_temp = float(
            self.coordinator.get_persistent_data(KEY_FROST_PROTECTION_TEMP, DEFAULT_FROST_PROTECTION_TEMP)
        )
        try:
            result = float(current_temp) < frost_temp
        except (ValueError, TypeError):
            result = False
        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, climate_id, event="frost_check", enabled=True,
            temperature=current_temp, source=source, trv_samples=trv_samples,
            sensor_fault=is_faulty, threshold=frost_temp, result=result,
        )
        return result

    async def async_handle_frost_emergencies(self) -> bool:
        """Evaluate frost emergency across all zones and trigger emergency heating if needed.
        
        Returns True if any zone triggered a frost emergency.
        """
        frost_emergency = False
        if not self.coordinator.get_persistent_data(KEY_ANTI_FROST_ENABLED, DEFAULT_ANTI_FROST_ENABLED):
            telemetry.get_recorder().record(telemetry.KIND_EVENT, "boiler", event="frost_scan", enabled=False)
            return False

        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, "boiler", event="frost_scan", phase="start",
            zones=[zone[CONF_ZONE_NAME] for zone in self.coordinator.zones],
        )
        for zone in self.coordinator.zones:
            climate_id = make_zone_entity_id(zone[CONF_ZONE_NAME])
            if self.is_zone_in_frost_emergency(climate_id):
                frost_emergency = True
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, climate_id, event="frost_action", phase="triggered",
                )
                _LOGGER.warning(
                    "Anti-Frost emergency triggered for zone %s! Forcing emergency heating.",
                    climate_id,
                )
                # Force zone demand to 100% so boiler PWM activates
                try:
                    self.coordinator.set_zone_demand(climate_id, 100.0)
                except asyncio.CancelledError:
                    telemetry.get_recorder().record(
                        telemetry.KIND_EVENT, climate_id, event="frost_action", phase="cancelled",
                        operation="set_demand",
                    )
                    raise
                except Exception as err:
                    telemetry.get_recorder().record(
                        telemetry.KIND_EVENT, climate_id, event="frost_action", phase="failed",
                        operation="set_demand", error=str(err),
                    )
                    raise
                # Force zone to HEAT mode if OFF
                st = self.hass.states.get(climate_id)
                mode = st.state if st else None
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, climate_id, event="frost_action", phase="mode_input",
                    available=bool(st), mode=mode,
                )
                if st and mode == HVAC_MODE_OFF:
                    try:
                        await self.coordinator._async_set_hvac_mode(climate_id, HVAC_MODE_HEAT)
                    except asyncio.CancelledError:
                        telemetry.get_recorder().record(
                            telemetry.KIND_EVENT, climate_id, event="frost_action", phase="cancelled",
                            operation="set_mode",
                        )
                        raise
                    except Exception as err:
                        telemetry.get_recorder().record(
                            telemetry.KIND_EVENT, climate_id, event="frost_action", phase="failed",
                            operation="set_mode", error=str(err),
                        )
                        raise
                    telemetry.get_recorder().record(
                        telemetry.KIND_EVENT, climate_id, event="frost_action", phase="heat_mode_requested",
                    )

                # Set target temperature above frost threshold so TRV internal PID opens valve
                frost_threshold = float(
                    self.coordinator.get_persistent_data(
                        KEY_FROST_PROTECTION_TEMP, DEFAULT_FROST_PROTECTION_TEMP
                    )
                )
                frost_target = frost_threshold + 3.0
                st = self.hass.states.get(climate_id)
                target = st.attributes.get(ATTR_TEMPERATURE) if st else None
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, climate_id, event="frost_action", phase="target_input",
                    available=bool(st), current_target=target, threshold=frost_threshold,
                    frost_target=frost_target,
                )
                if not st or target != frost_target:
                    try:
                        await self.hass.services.async_call(
                            CLIMATE_DOMAIN,
                            SERVICE_SET_TEMPERATURE,
                            {ATTR_ENTITY_ID: climate_id, ATTR_TEMPERATURE: frost_target},
                            blocking=False,
                        )
                    except asyncio.CancelledError:
                        telemetry.get_recorder().record(
                            telemetry.KIND_EVENT, climate_id, event="frost_action", phase="cancelled",
                            operation="set_target",
                        )
                        raise
                    except Exception as err:
                        telemetry.get_recorder().record(
                            telemetry.KIND_EVENT, climate_id, event="frost_action", phase="failed",
                            operation="set_target", error=str(err),
                        )
                        raise
                    telemetry.get_recorder().record(
                        telemetry.KIND_EVENT, climate_id, event="frost_action", phase="target_requested",
                        target=frost_target,
                    )

        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, "boiler", event="frost_scan", phase="completed",
            enabled=True, result=frost_emergency,
        )
        return frost_emergency
