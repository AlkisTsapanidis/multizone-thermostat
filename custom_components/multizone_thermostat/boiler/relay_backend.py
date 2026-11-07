"""Relay boiler backend with PWM control."""
import logging
import time
import asyncio
from contextvars import ContextVar
from collections.abc import Mapping
from copy import deepcopy
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    STATE_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)

from .base import BoilerBackend
from .. import telemetry
from ..pwm_engine import PWMEngine
from ..const import CONF_ZONE_NAME, CONF_ZONE_CLIMATES, CONF_ZONE_SWITCHES, ZONE_MODE_PRIMARY, make_zone_entity_id

_LOGGER = logging.getLogger(__name__)

COMMAND_CONFIRM_TIMEOUT = 60.0  # seconds to wait for the switch entity to confirm a command

class RelayBoilerBackend(BoilerBackend):
    """Handles Relay/On-Off boilers via PWM."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        boiler_switch: str,
        min_cycle_on: float,
        min_cycle_off: float,
        valve_delay: float,
    ) -> None:
        """Initialize Relay backend."""
        self.hass = hass
        self.coordinator = coordinator
        self.boiler_switch = boiler_switch
        self._min_cycle_on = min_cycle_on
        self._min_cycle_off = min_cycle_off
        self._valve_delay = valve_delay

        self._boiler_pwm = PWMEngine(pwm_interval=900.0, min_on=self._min_cycle_on * 60, min_off=self._min_cycle_off * 60)
        self._last_boiler_change = 0.0
        self._pending_boiler_task: asyncio.Task | None = None
        self._status_reason = "INIT"
        self._last_demand = 0.0
        self._last_frost_emergency = False
        # Command confirmation tracking: None | ("on"/"off", monotonic_ts)
        self._pending_command: tuple[str, float] | None = None
        self._last_known_boiler_on: bool = False
        self._trv_wait_start: float | None = None
        self._delay_epoch = uuid4().hex
        self._delay_step = 0
        self._replay_trace = ContextVar("relay_replay_trace", default=None)

    def _read_state(self, entity):
        state = self.hass.states.get(entity)
        trace = self._replay_trace.get()
        if trace is not None:
            try:
                attrs = getattr(state, "attributes", None)
                trace.append({
                    "type": "state", "entity": entity, "exists": state is not None,
                    "state": state.state if state is not None else None,
                    "attributes": deepcopy({k: attrs[k] for k in (
                        "position", "valve_position", "pi_heating_demand", "heating_demand", "valve_state",
                    ) if k in attrs}) if isinstance(attrs, Mapping) else {},
                })
            except Exception:
                _LOGGER.exception("Unable to capture relay state for replay")
                trace.append({"type": "capture_error"})
        return state

    def _clock(self):
        value = time.monotonic()
        trace = self._replay_trace.get()
        if trace is not None:
            trace.append({"type": "clock", "value": value})
        return value

    def _zone_value(self, kind, climate_id):
        getter = self.coordinator.get_zone_mode if kind == "mode" else self.coordinator.get_zone_demand
        value = getter(climate_id)
        trace = self._replay_trace.get()
        if trace is not None:
            trace.append({"type": kind, "zone": climate_id, "value": value})
        return value

    async def _call_switch(self, service, data, **kwargs):
        trace = self._replay_trace.get()
        entry = None
        if trace is not None:
            entry = {"type": "service", "service": service, "data": data.copy(),
                     "blocking": kwargs.get("blocking"), "outcome": "pending"}
            trace.append(entry)
        try:
            result = await self.hass.services.async_call("switch", service, data, **kwargs)
        except asyncio.CancelledError:
            if entry is not None:
                entry["outcome"] = "cancelled"
            raise
        except Exception as err:
            if entry is not None:
                entry.update(outcome="error", error=str(err))
            raise
        if entry is not None:
            entry["outcome"] = "completed"
        return result

    @property
    def status_reason(self) -> str:
        """Return the current status reason."""
        return self._status_reason

    def set_min_cycle_on(self, value: int) -> None:
        """Set the min cycle on time (minutes)."""
        self._min_cycle_on = value
        self._boiler_pwm.set_params(min_on=value * 60)

    def set_min_cycle_off(self, value: int) -> None:
        """Set the min cycle off time (minutes)."""
        self._min_cycle_off = value
        self._boiler_pwm.set_params(min_off=value * 60)

    def set_valve_delay(self, value: int) -> None:
        """Set the valve delay time (seconds)."""
        self._valve_delay = value

    async def _trace_delayed_command(self, turn_on: bool, epoch: str, step: int) -> None:
        recorder = telemetry.get_recorder()
        trace = [] if recorder.enabled else None
        before = {
            "switch": self.boiler_switch, "last_change": self._last_boiler_change,
            "pending_command": self._pending_command, "reason": self._status_reason,
        } if trace is not None else None
        token = self._replay_trace.set(trace)
        completed = False
        cancelled = False
        error = None
        try:
            if turn_on:
                await self._force_boiler_on()
            else:
                await self._force_boiler_off()
            completed = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as err:
            error = str(err)
            raise
        finally:
            self._replay_trace.reset(token)
            recorder.record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay_command", epoch=epoch, step=step,
                turn_on=turn_on, before=before,
                after={"last_change": self._last_boiler_change, "pending_command": self._pending_command,
                       "reason": self._status_reason} if trace is not None else None,
                trace=[item.copy() for item in trace] if trace is not None else None,
                completed=completed, cancelled=cancelled, error=error,
            )

    def _schedule_boiler_check(self, delay_seconds: float, turn_on: bool) -> None:
        """Schedule a delayed boiler update (used for valve delay)."""
        if self._pending_boiler_task:
            self._pending_boiler_task.cancel()

        step = self._delay_step
        self._delay_step += 1
        def record_delay(phase, **data):
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay", phase=phase,
                epoch=self._delay_epoch, step=step, **data,
            )

        record_delay("scheduled", seconds=delay_seconds, turn_on=turn_on, reason_before=self._status_reason)

        async def _delayed_check():
            try:
                record_delay("waiting")
                await asyncio.sleep(delay_seconds)
                record_delay("expired")
                if turn_on:
                    valid = self._conditions_still_valid((self._delay_epoch, step))
                    record_delay("rechecked", valid=valid)
                    if not valid:
                        _LOGGER.debug("Valve delay expired but heat request no longer valid. Boiler stays OFF.")
                        self._status_reason = "OFF - Heat request ended during valve delay"
                        return
                    self._status_reason = f"ON - Demand {round(self._last_demand, 1)}% (after valve delay)"
                    record_delay("on_request_started", demand=self._last_demand)
                    await self._trace_delayed_command(True, self._delay_epoch, step)
                    record_delay("on_request_completed")
                else:
                    record_delay("off_request_started")
                    await self._trace_delayed_command(False, self._delay_epoch, step)
                    record_delay("off_request_completed")
            except asyncio.CancelledError:
                record_delay("cancelled")
                pass
            except Exception as err:
                record_delay("error", error=str(err))
                raise
            finally:
                if self._pending_boiler_task is asyncio.current_task():
                    self._pending_boiler_task = None
                record_delay("finished", reason=self._status_reason)

        self._pending_boiler_task = self.hass.async_create_task(_delayed_check())

    def _switch_valve_path_open(self) -> bool | None:
        """Check demanding switch-valve zones (no TRVs).

        Returns None if there are none, True if any such valve switch is ON,
        False if every one of them is closed.
        """
        recorder = telemetry.get_recorder()
        observed = [] if recorder.enabled else None
        def finish(result):
            if observed is not None:
                recorder.record(telemetry.KIND_EVENT, "boiler", event="relay_switch_path",
                                zones=observed, result=result)
            return result

        found = False
        for zone in self.coordinator.zones:
            climate_id = make_zone_entity_id(zone[CONF_ZONE_NAME])
            mode = self._zone_value("mode", climate_id)
            sample = {"name": zone[CONF_ZONE_NAME], "id": climate_id, "mode": mode} if observed is not None else None
            if sample is not None:
                observed.append(sample)
            if mode != ZONE_MODE_PRIMARY:
                continue
            demand = self._zone_value("demand", climate_id)
            if sample is not None:
                sample["demand"] = demand
            if not demand > 0:
                continue
            trvs = zone.get(CONF_ZONE_CLIMATES) or zone.get("climates") or zone.get("climate_entities") or []
            switches = zone.get(CONF_ZONE_SWITCHES) or []
            if sample is not None:
                sample["trvs"] = list(trvs) if isinstance(trvs, (list, tuple)) else None
                sample["switches"] = list(switches) if isinstance(switches, (list, tuple)) else None
                sample["states"] = []
            if trvs or not switches:
                continue
            found = True
            for sw in switches:
                st = self._read_state(sw)
                state = st.state if st is not None else None
                if sample is not None:
                    sample["states"].append({"entity": sw, "state": state})
                if st is not None and state == STATE_ON:
                    return finish(True)
        return finish(False if found else None)

    def _conditions_still_valid(self, replay_context: tuple[str, int] | None = None) -> bool:
        """Re-check Master, DHW, demand and valve path before igniting after valve delay."""
        context = {"epoch": replay_context[0], "step": replay_context[1]} if replay_context else {}
        if self._last_frost_emergency:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay_check", frost=True, result=True, **context,
            )
            return True
        master_on = self.coordinator.get_master_state()
        if not master_on:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay_check", frost=False, master_on=master_on,
                result=False, **context,
            )
            return False
        dhw_active = self.coordinator.is_dhw_active
        if dhw_active:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay_check", frost=False, master_on=master_on,
                dhw_active=dhw_active, result=False, **context,
            )
            return False
        switch_path = self._switch_valve_path_open()
        trv_path = getattr(self, "_trv_path_ready", False) if switch_path is False else None
        if switch_path is False and not trv_path:
            telemetry.get_recorder().record(
                telemetry.KIND_EVENT, "boiler", event="relay_delay_check", frost=False, master_on=master_on,
                dhw_active=dhw_active, switch_path=switch_path, trv_path=trv_path, result=False, **context,
            )
            return False
        result = self._last_demand > 0.0
        telemetry.get_recorder().record(
            telemetry.KIND_EVENT, "boiler", event="relay_delay_check", frost=False, master_on=master_on,
            dhw_active=dhw_active, switch_path=switch_path, trv_path=trv_path,
            demand=self._last_demand, result=result, **context,
        )
        return result

    async def _force_boiler_on(self) -> None:
        """Actually turn the boiler on and set locks."""
        if not self.boiler_switch:
            return

        boiler_state = self._read_state(self.boiler_switch)
        if boiler_state is None or boiler_state.state == STATE_UNAVAILABLE:
            _LOGGER.warning("Boiler switch %s is unavailable, skipping ON command", self.boiler_switch)
            return

        was_off = boiler_state.state != STATE_ON
        was_off = boiler_state.state != STATE_ON
        has_pending_off = self._pending_command is not None and self._pending_command[0] == "off"

        if not was_off and not has_pending_off:
            _LOGGER.debug("Boiler switch %s is already ON, skipping redundant turn_on command", self.boiler_switch)
            return

        kwargs = {"blocking": False}
        from unittest.mock import MagicMock
        if not isinstance(self.hass, MagicMock):
            from homeassistant.core import Context
            kwargs["context"] = Context()

        await self._call_switch(
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: self.boiler_switch},
            **kwargs,
        )
        if was_off or has_pending_off:
            self._last_boiler_change = self._clock()
            self._pending_command = ("on", self._clock())

            if not isinstance(self.hass, MagicMock) and self.hass.services.has_service("logbook", "log"):
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "logbook",
                        "log",
                        {
                            "name": "Multizone Thermostat",
                            "message": f"Turned ON boiler ({self._status_reason})",
                            "entity_id": self.boiler_switch,
                            "domain": "multizone_thermostat",
                        },
                        blocking=False,
                    )
                )

            if hasattr(self.coordinator, "reset_last_active_time"):
                self.coordinator.reset_last_active_time()
            self.coordinator.record_diagnostic_event("BOILER_SWITCH_ON", {"switch": self.boiler_switch, "reason": self._status_reason})
            _LOGGER.debug("Boiler forced ON")

    async def _force_boiler_off(self) -> None:
        """Actually turn the boiler off and set locks."""
        if not self.boiler_switch:
            return

        boiler_state = self._read_state(self.boiler_switch)
        if boiler_state is None:
            _LOGGER.warning("Boiler switch %s not found, cannot send OFF", self.boiler_switch)
            return

        # An OFF command is always attempted unless the switch is known to be off:
        # after a restart the state is 'unknown' and the relay may really be on.
        was_on = boiler_state.state != "off"
        has_pending_on = self._pending_command is not None and self._pending_command[0] == "on"

        if not was_on and not has_pending_on:
            _LOGGER.debug("Boiler switch %s is already OFF, skipping redundant turn_off command", self.boiler_switch)
            return

        kwargs = {"blocking": False}
        from unittest.mock import MagicMock
        if not isinstance(self.hass, MagicMock):
            from homeassistant.core import Context
            kwargs["context"] = Context()

        await self._call_switch(
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: self.boiler_switch},
            **kwargs,
        )
        if was_on or has_pending_on:
            self._last_boiler_change = self._clock()
            self._pending_command = ("off", self._clock())

            if not isinstance(self.hass, MagicMock) and self.hass.services.has_service("logbook", "log"):
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "logbook",
                        "log",
                        {
                            "name": "Multizone Thermostat",
                            "message": f"Turned OFF boiler ({self._status_reason})",
                            "entity_id": self.boiler_switch,
                            "domain": "multizone_thermostat",
                        },
                        blocking=False,
                    )
                )

            self.coordinator.record_diagnostic_event("BOILER_SWITCH_OFF", {"switch": self.boiler_switch, "reason": self._status_reason})
            _LOGGER.debug("Boiler forced OFF")

    async def async_emergency_off(self) -> None:
        """Fallback for emergency off and anti-seize."""
        recorder = telemetry.get_recorder()
        trace = [] if recorder.enabled else None
        before = {
            "switch": self.boiler_switch, "last_change": self._last_boiler_change,
            "pending_command": self._pending_command, "last_known_on": self._last_known_boiler_on,
            "trv_wait_start": self._trv_wait_start, "delay_pending": self._pending_boiler_task is not None,
            "reason": self._status_reason,
        } if trace is not None else None
        token = self._replay_trace.set(trace)
        completed = False
        cancelled = False
        error = None
        try:
            await self._async_emergency_off_impl()
            completed = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as err:
            error = str(err)
            raise
        finally:
            self._replay_trace.reset(token)
            recorder.record(
                telemetry.KIND_EVENT, "boiler", event="relay_emergency_off", before=before,
                after={
                    "last_change": self._last_boiler_change, "pending_command": self._pending_command,
                    "last_known_on": self._last_known_boiler_on, "trv_wait_start": self._trv_wait_start,
                    "delay_pending": self._pending_boiler_task is not None, "reason": self._status_reason,
                } if trace is not None else None,
                trace=[item.copy() for item in trace] if trace is not None else None,
                completed=completed, cancelled=cancelled, error=error,
            )

    async def _async_emergency_off_impl(self) -> None:
        """Perform the emergency-off operation."""
        if self._pending_boiler_task:
            self._pending_boiler_task.cancel()
            self._pending_boiler_task = None
        self._trv_wait_start = None
        self._status_reason = "OFF - Emergency/Master OFF"
        boiler_state = self._read_state(self.boiler_switch) if self.boiler_switch else None
        if boiler_state is None:
            _LOGGER.warning("Boiler switch %s not found, cannot send emergency OFF", self.boiler_switch)
            return
        has_pending_on = self._pending_command is not None and self._pending_command[0] == "on"
        if boiler_state.state != "off" or has_pending_on:
            await self._force_boiler_off()

    async def async_update(self, demand: float, peak_zone: str | None, frost_emergency: bool) -> None:
        """Feed Peak Load to PWM Engine and control the relay (recording telemetry around it)."""
        pwm = self._boiler_pwm
        recorder = telemetry.get_recorder()
        trace = [] if recorder.enabled else None
        replay_before = None
        if trace is not None:
            try:
                replay_before = {
                    "switch": self.boiler_switch, "min_cycle_on": self._min_cycle_on,
                    "min_cycle_off": self._min_cycle_off, "valve_delay": self._valve_delay,
                    "last_change": self._last_boiler_change, "pending_command": self._pending_command,
                    "last_known_on": self._last_known_boiler_on, "trv_wait_start": self._trv_wait_start,
                    "delay_pending": self._pending_boiler_task is not None,
                    "dhw_active": None, "diagnostic_on": None, "zones": [],
                }
            except Exception:
                _LOGGER.exception("Unable to capture relay replay inputs")
        trace_token = self._replay_trace.set(trace)
        pre = {
            "cycle_start_before": pwm.cycle_start_time,
            "prev_demand_before": getattr(pwm, "_prev_demand", 0.0),
            "reason_before": self._status_reason,
        }
        calculation = {"replay_before": replay_before}
        update_completed = False
        update_cancelled = False
        update_error = None
        try:
            await self._async_update_impl(demand, peak_zone, frost_emergency, calculation)
            update_completed = True
        except asyncio.CancelledError:
            update_cancelled = True
            raise
        except Exception as err:
            update_error = str(err)
            raise
        finally:
            self._replay_trace.reset(trace_token)
            boiler_state = self.hass.states.get(self.boiler_switch) if self.boiler_switch else None
            recorder.record(
                telemetry.KIND_BOILER, "boiler",
                demand=demand, peak_zone=peak_zone, frost_emergency=frost_emergency,
                state=pwm.current_state, time_on=pwm.time_on, time_off=pwm.time_off,
                pwm_interval=pwm.pwm_interval, min_on=pwm.min_on, min_off=pwm.min_off,
                switch_state=getattr(boiler_state, "state", None),
                pending_command=self._pending_command[0] if self._pending_command else None,
                since_last_change=time.monotonic() - self._last_boiler_change,
                reason=self._status_reason, calculation_time=calculation.get("time"),
                pwm_epoch=calculation.get("epoch"), pwm_step=calculation.get("step"),
                calculation_state=calculation.get("state"), calculation_time_on=calculation.get("time_on"),
                calculation_time_off=calculation.get("time_off"), calculation_interval=calculation.get("interval"),
                calculation_min_on=calculation.get("min_on"), calculation_min_off=calculation.get("min_off"), **pre,
                update_completed=update_completed, update_cancelled=update_cancelled, update_error=update_error,
                replay_before=replay_before,
                replay_after={
                    "last_change": self._last_boiler_change, "pending_command": self._pending_command,
                    "last_known_on": self._last_known_boiler_on, "trv_wait_start": self._trv_wait_start,
                    "delay_pending": self._pending_boiler_task is not None,
                } if trace is not None else None,
                replay_trace=[item.copy() for item in trace] if trace is not None else None,
            )

    async def _async_update_impl(
        self, demand: float, peak_zone: str | None, frost_emergency: bool, calculation: dict,
    ) -> None:
        self._last_demand = demand
        self._last_frost_emergency = frost_emergency
        wanted_state = self._boiler_pwm.calculate(demand)
        calculation["time"] = self._boiler_pwm.last_calculation_time
        calculation["epoch"] = self._boiler_pwm.replay_epoch
        calculation["step"] = self._boiler_pwm.replay_step - 1
        calculation["state"] = wanted_state
        calculation["time_on"] = self._boiler_pwm.time_on
        calculation["time_off"] = self._boiler_pwm.time_off
        calculation["interval"] = self._boiler_pwm.pwm_interval
        calculation["min_on"] = self._boiler_pwm.min_on
        calculation["min_off"] = self._boiler_pwm.min_off

        boiler_state = self._read_state(self.boiler_switch) if self.boiler_switch else None
        current_boiler_on = boiler_state is not None and boiler_state.state == STATE_ON

        if current_boiler_on and hasattr(self.coordinator, "reset_last_active_time"):
            self.coordinator.reset_last_active_time()

        if current_boiler_on and not self._last_known_boiler_on:
            if self._pending_command is None or self._pending_command[0] != "on":
                self._last_boiler_change = self._clock()
        elif not current_boiler_on and self._last_known_boiler_on:
            if self._pending_command is None or self._pending_command[0] != "off":
                self._last_boiler_change = self._clock()
        self._last_known_boiler_on = current_boiler_on

        # Diagnostics follow the CONFIRMED entity state, not the command sent
        diag = self.coordinator.plant_diagnostics
        replay_before = calculation.get("replay_before")
        if replay_before is not None:
            try:
                replay_before["diagnostic_on"] = diag._current_boiler_start is not None
            except Exception:
                _LOGGER.exception("Unable to capture relay diagnostic state for replay")
        if current_boiler_on and diag._current_boiler_start is None:
            diag.record_boiler_state(True)
        elif boiler_state is not None and boiler_state.state == STATE_OFF and diag._current_boiler_start is not None:
            diag.record_boiler_state(False)
        if self._pending_command is not None:
            cmd, ts = self._pending_command
            if (cmd == "on" and current_boiler_on) or (cmd == "off" and boiler_state is not None and not current_boiler_on):
                self._pending_command = None
            elif self._clock() - ts > COMMAND_CONFIRM_TIMEOUT:
                _LOGGER.warning("Boiler switch %s did not confirm '%s' within %ss", self.boiler_switch, cmd, COMMAND_CONFIRM_TIMEOUT)
                self.coordinator.record_diagnostic_event("BOILER_COMMAND_TIMEOUT", {"switch": self.boiler_switch, "command": cmd})
                self._pending_command = None

        # DHW priority: heating request is suppressed, so release the relay even if min_cycle_on is active
        dhw_active = bool(getattr(self.coordinator, "is_dhw_active", False))
        if replay_before is not None:
            replay_before["dhw_active"] = dhw_active
        dhw_override = dhw_active and not frost_emergency

        if wanted_state and not current_boiler_on:
            time_since_change = self._clock() - self._last_boiler_change
            min_off_sec = self._min_cycle_off * 60
            if time_since_change < min_off_sec:
                self._status_reason = f"HOLD_OFF - min_cycle_off active ({round(min_off_sec - time_since_change)}s remaining)"
                _LOGGER.debug("PWM Tick: Boiler wants to turn ON, but min_cycle_off (%.0fs) hasn't elapsed. Waiting...", min_off_sec)
                return

            _LOGGER.debug("PWM Tick: Boiler -> ON (Peak Demand: %.1f%%)", demand)

            trvs_ready = False
            zones = self.coordinator.zones
            if replay_before is not None:
                try:
                    if not isinstance(zones, (list, tuple)):
                        replay_before["zones"] = None
                        _LOGGER.warning("Unable to capture non-list relay zone configuration for replay")
                    else:
                        replay_before["zones"] = deepcopy([
                            {key: zone[key] for key in (
                                CONF_ZONE_NAME, CONF_ZONE_CLIMATES, CONF_ZONE_SWITCHES,
                                "climates", "climate_entities",
                            ) if key in zone} for zone in zones
                        ])
                except Exception:
                    _LOGGER.exception("Unable to capture relay zone configuration for replay")
                    replay_before["zones"] = None
            for zone in zones:
                climate_id = make_zone_entity_id(zone[CONF_ZONE_NAME])
                mode = self._zone_value("mode", climate_id)
                if mode == ZONE_MODE_PRIMARY and self._zone_value("demand", climate_id) > 0:
                    trv_list = zone.get(CONF_ZONE_CLIMATES) or zone.get("climates") or zone.get("climate_entities") or []
                    if not trv_list:
                        # Switch-valve zones are judged by _switch_valve_path_open below.
                        if not (zone.get(CONF_ZONE_SWITCHES) or []):
                            trvs_ready = True
                            break
                        continue
                    
                    for trv in trv_list:
                        trv_state = self._read_state(trv)
                        if trv_state and trv_state.state not in ("unavailable", "unknown"):
                            position = None
                            for attr in ["position", "valve_position", "pi_heating_demand", "heating_demand", "valve_state"]:
                                if attr in trv_state.attributes:
                                    position = trv_state.attributes[attr]
                                    break
                            if position is not None:
                                try:
                                    if float(position) > 10.0:
                                        trvs_ready = True
                                        break
                                except (ValueError, TypeError):
                                    if isinstance(position, str) and position.lower() in ("open", "heating", "on", "true"):
                                        trvs_ready = True
                                        break
                            else:
                                trvs_ready = True
                                break
                if trvs_ready:
                    break

            self._trv_path_ready = trvs_ready
            switch_path = self._switch_valve_path_open()
            if switch_path is True:
                trvs_ready = True
            elif switch_path is False and not trvs_ready and not frost_emergency and peak_zone != "Anti-Seize":
                self._status_reason = "HOLD_VALVES_CLOSED - Waiting for a zone valve to open"
                _LOGGER.debug("PWM Tick: Boiler wants to turn ON, but all demanding zone valves are closed.")
                return

            has_any_trvs = any(
                (zone.get(CONF_ZONE_CLIMATES) or zone.get("climates") or zone.get("climate_entities") or [])
                for zone in self.coordinator.zones
            )
            if not trvs_ready and has_any_trvs and not frost_emergency and peak_zone != "Anti-Seize":
                if self._trv_wait_start is None:
                    self._trv_wait_start = self._clock()
                elapsed = self._clock() - self._trv_wait_start
                if elapsed < 120.0:
                    self._status_reason = f"HOLD_TRV_CLOSED - Waiting for TRVs to open ({round(120.0 - elapsed)}s remaining)"
                    _LOGGER.warning("PWM Tick: Boiler wants to turn ON, but no TRVs are open > 10%%. Waiting for TRVs to open (%.0fs/120s)", elapsed)
                    return
                _LOGGER.info("PWM Tick: TRV opening grace period (120s) elapsed. Proceeding with boiler ignition.")
                trvs_ready = True

            self._trv_wait_start = None

            if self._valve_delay > 0:
                if not self._pending_boiler_task:
                    self._status_reason = f"WAITING_VALVES - Valve delay active ({self._valve_delay}s)"
                    _LOGGER.debug("Waiting %s seconds for valves to open...", self._valve_delay)
                    self._schedule_boiler_check(self._valve_delay, True)
            else:
                self._status_reason = f"ON - Demand {round(demand, 1)}% from {peak_zone or 'Zone'}"
                await self._force_boiler_on()
        elif not wanted_state:
            self._trv_wait_start = None
            time_since_change = self._clock() - self._last_boiler_change
            min_on_sec = self._min_cycle_on * 60
            if current_boiler_on and time_since_change < min_on_sec and not dhw_override:
                self._status_reason = f"HOLD_ON - min_cycle_on active ({round(min_on_sec - time_since_change)}s remaining)"
                _LOGGER.debug("PWM Tick: Boiler wants to turn OFF, but min_cycle_on (%.0fs) hasn't elapsed. Waiting...", min_on_sec)
                return

            if self._pending_boiler_task:
                _LOGGER.debug("PWM Tick: Cancelling pending valve-delay task (Peak Demand: %.1f%%)", demand)
                self._pending_boiler_task.cancel()
                self._pending_boiler_task = None
            if current_boiler_on:
                self._status_reason = f"OFF - Cycle ended (Peak Demand: {round(demand, 1)}%)"
                _LOGGER.debug("PWM Tick: Boiler -> OFF (Peak Demand: %.1f%%)", demand)
                await self._force_boiler_off()
            else:
                self._status_reason = f"OFF - Idle (Peak Demand: {round(demand, 1)}%)"
        else:
            self._status_reason = f"ON - Heating (Peak Demand: {round(demand, 1)}% from {peak_zone or 'Zone'})"
