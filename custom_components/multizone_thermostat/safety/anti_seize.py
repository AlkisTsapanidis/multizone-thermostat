"""Anti-seize safety module for seasonal valve and boiler cycling."""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from .. import telemetry
from ..const import (
    CONF_ANTI_SEIZE_BOILER,
    CONF_ZONE_ANTI_SEIZE,
    CONF_ZONE_NAME,
    HVAC_MODE_HEAT,
    KEY_ANTI_SEIZE_DURATION,
    KEY_ANTI_SEIZE_ENABLED,
    KEY_ANTI_SEIZE_IDLE_DAYS,
    make_zone_entity_id,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class AntiSeizeManager:
    """Manages periodic cycling of valves and boiler to prevent mechanical seizing."""

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        """Initialize anti-seize manager."""
        self.hass = hass
        self.coordinator = coordinator
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._pre_anti_seize_state: dict[str, str] = {}
        self._pre_anti_seize_demand: dict[str, float] = {}

    @property
    def is_running(self) -> bool:
        """Return True if anti-seize cycle is currently running."""
        return self._running

    def check_anti_seize(self, now: datetime | None = None) -> None:
        """Check if we need to run the anti-seize routine."""
        if not self.coordinator.get_persistent_data(KEY_ANTI_SEIZE_ENABLED, False):
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize_check", enabled=False,
            )
            return

        if self._running:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize_check", enabled=True, running=True,
            )
            return

        idle_seconds = time.monotonic() - self.coordinator.last_active_time
        idle_days = idle_seconds / 86400.0
        anti_seize_idle_days = self.coordinator.get_persistent_data(KEY_ANTI_SEIZE_IDLE_DAYS, 15)
        triggered = idle_days >= anti_seize_idle_days
        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, "boiler", event="anti_seize_check", enabled=True, running=False,
            idle_days=idle_days, threshold_days=anti_seize_idle_days,
            triggered=triggered,
        )

        if triggered:
            _LOGGER.info(
                "Anti-seize triggered. System idle for %.1f days (threshold: %d days).",
                idle_days,
                anti_seize_idle_days,
            )
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="scheduled", trigger="automatic",
            )
            self._task = self.hass.async_create_task(self.async_execute())

    def trigger_manually(self) -> asyncio.Task:
        """Manually trigger anti-seize routine."""
        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="scheduled", trigger="manual",
        )
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = self.hass.async_create_task(self.async_execute())
        return self._task

    async def _turn_boiler_on(self) -> None:
        """Turn on boiler for anti-seize."""
        if hasattr(self.coordinator, "boiler_backend") and self.coordinator.boiler_backend:
            await self.coordinator.boiler_backend.async_update(100.0, "Anti-Seize", False)
        elif getattr(self.coordinator, "boiler_switch", None):
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: self.coordinator.boiler_switch},
                blocking=False,
            )

    async def _turn_boiler_off(self) -> None:
        """Turn off boiler after anti-seize."""
        if hasattr(self.coordinator, "boiler_backend") and self.coordinator.boiler_backend:
            await self.coordinator.boiler_backend.async_update(0.0, None, False)
            status_reason = getattr(self.coordinator.boiler_backend, "_status_reason", None) or getattr(
                self.coordinator.boiler_backend, "status_reason", ""
            )
            hold_on = isinstance(status_reason, str) and status_reason.startswith("HOLD_ON")
            emergency_supported = hold_on and hasattr(self.coordinator.boiler_backend, "async_emergency_off")
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="boiler_off_status",
                status_reason=status_reason if isinstance(status_reason, str) else None,
                emergency_supported=bool(emergency_supported),
            )
            if emergency_supported:
                await self.coordinator.boiler_backend.async_emergency_off()
        elif getattr(self.coordinator, "boiler_switch", None):
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="boiler_off_switch",
            )
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: self.coordinator.boiler_switch},
                blocking=False,
            )

    async def async_execute(self) -> None:
        """Execute the anti-seize routine."""
        self._running = True
        telemetry.get_recorder().record(telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="start")
        stage = {"operation": "selection"}

        try:
            _LOGGER.info("Starting Anti-seize routine...")

            # 1. Save current state of all enabled zones and open them
            self._pre_anti_seize_state.clear()
            self._pre_anti_seize_demand.clear()
            zones_to_open = []
            observed_zones = []

            for zone in self.coordinator.zones:
                climate_id = make_zone_entity_id(zone[CONF_ZONE_NAME])
                if not zone.get(CONF_ZONE_ANTI_SEIZE, True):
                    observed_zones.append({"name": zone[CONF_ZONE_NAME], "enabled": False, "available": None})
                    _LOGGER.debug("Skipping zone %s (anti-seize disabled)", climate_id)
                    continue

                state = self.hass.states.get(climate_id)
                observed_zones.append({"name": zone[CONF_ZONE_NAME], "enabled": True, "available": bool(state)})
                if state:
                    self._pre_anti_seize_state[climate_id] = state.state
                    if hasattr(self.coordinator, "get_zone_demand"):
                        try:
                            self._pre_anti_seize_demand[climate_id] = float(
                                self.coordinator.get_zone_demand(climate_id)
                            )
                        except (ValueError, TypeError):
                            self._pre_anti_seize_demand[climate_id] = 0.0
                    zones_to_open.append(climate_id)

            if not zones_to_open:
                _LOGGER.info("No zones enabled for anti-seize. Skipping routine.")
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="no_zones", zone_inputs=observed_zones,
                )
                self.coordinator.reset_last_active_time()
                return

            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="zones_selected",
                zones=zones_to_open.copy(), zone_inputs=observed_zones,
                saved_states=self._pre_anti_seize_state.copy(),
                saved_demands=self._pre_anti_seize_demand.copy(),
            )
            _LOGGER.debug("Opening zones: %s", zones_to_open)
            for climate_id in zones_to_open:
                stage = {"operation": "zone_open_mode", "zone": climate_id}
                await self.coordinator._async_set_hvac_mode(climate_id, HVAC_MODE_HEAT)
                if hasattr(self.coordinator, "set_zone_demand"):
                    stage = {"operation": "zone_open_demand", "zone": climate_id}
                    self.coordinator.set_zone_demand(climate_id, 100.0)
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, climate_id, event="anti_seize", phase="zone_open_requested",
                )

            # 2. Wait for valves to open
            valve_delay = getattr(self.coordinator, "_valve_delay", 0)
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="valve_wait", seconds=valve_delay,
            )
            if valve_delay > 0:
                _LOGGER.debug("Waiting %s seconds for valves to open...", valve_delay)
                stage = {"operation": "valve_wait"}
                await asyncio.sleep(valve_delay)

            # 3. Fire boiler if configured
            anti_seize_boiler = self.coordinator.get_persistent_data(CONF_ANTI_SEIZE_BOILER, False)
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="boiler_decision",
                enabled=anti_seize_boiler, valve_delay=valve_delay,
            )
            if anti_seize_boiler:
                _LOGGER.debug("Activating boiler for anti-seize...")
                stage = {"operation": "boiler_on"}
                await self._turn_boiler_on()
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="boiler_on_requested",
                )

            # 4. Wait for duration
            anti_seize_duration = self.coordinator.get_persistent_data(KEY_ANTI_SEIZE_DURATION, 2)
            duration_seconds = anti_seize_duration * 60
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="duration",
                minutes=anti_seize_duration, seconds=duration_seconds,
            )
            _LOGGER.debug("Running anti-seize for %d seconds...", duration_seconds)
            stage = {"operation": "duration_wait"}
            await asyncio.sleep(duration_seconds)

            # 5. Turn off boiler if we turned it on
            if anti_seize_boiler:
                _LOGGER.debug("Deactivating boiler after anti-seize...")
                stage = {"operation": "boiler_off"}
                await self._turn_boiler_off()
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="boiler_off_requested",
                )

            # 6. Restore all zones
            _LOGGER.debug("Restoring zones to pre-anti-seize state...")
            for climate_id, old_state in self._pre_anti_seize_state.items():
                if hasattr(self.coordinator, "set_zone_demand") and climate_id in self._pre_anti_seize_demand:
                    stage = {"operation": "zone_restore_demand", "zone": climate_id}
                    self.coordinator.set_zone_demand(climate_id, self._pre_anti_seize_demand[climate_id])
                stage = {"operation": "zone_restore_mode", "zone": climate_id}
                await self.coordinator._async_set_hvac_mode(climate_id, old_state)
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, climate_id, event="anti_seize", phase="zone_restored",
                    state=old_state, demand=self._pre_anti_seize_demand.get(climate_id),
                )

            _LOGGER.info("Anti-seize routine completed successfully.")
            telemetry.get_recorder().record(telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="completed")
            self.coordinator.reset_last_active_time()

        except asyncio.CancelledError:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="cancelled", stage=stage,
            )
            _LOGGER.warning("Anti-seize routine was cancelled! Initiating emergency shutdown of boiler and valves.")

            async def _emergency_cleanup():
                try:
                    await self._turn_boiler_off()
                    for climate_id, old_state in self._pre_anti_seize_state.items():
                        if hasattr(self.coordinator, "set_zone_demand") and climate_id in self._pre_anti_seize_demand:
                            self.coordinator.set_zone_demand(climate_id, self._pre_anti_seize_demand[climate_id])
                        await self.coordinator._async_set_hvac_mode(climate_id, old_state)
                except Exception:
                    telemetry.get_recorder().record(
                        telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="cancel_cleanup_error",
                    )
                    raise
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="cancel_cleanup_completed",
                )

            self.hass.async_create_task(_emergency_cleanup())
            raise
        except Exception as e:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="error", error=str(e),
                stage=stage,
            )
            _LOGGER.error("Error during anti-seize routine: %s", e)
            try:
                await self._turn_boiler_off()
                for climate_id, old_state in self._pre_anti_seize_state.items():
                    if hasattr(self.coordinator, "set_zone_demand") and climate_id in self._pre_anti_seize_demand:
                        self.coordinator.set_zone_demand(climate_id, self._pre_anti_seize_demand[climate_id])
                    await self.coordinator._async_set_hvac_mode(climate_id, old_state)
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="cleanup_completed",
                )
            except Exception as cleanup_err:
                telemetry.get_recorder().record(
                    telemetry.KIND_EVENT, "boiler", event="anti_seize", phase="cleanup_error", error=str(cleanup_err),
                )
                _LOGGER.error("Error during anti-seize emergency cleanup: %s", cleanup_err)
        finally:
            self._running = False

    def stop(self) -> None:
        """Cancel running anti-seize task if any."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
