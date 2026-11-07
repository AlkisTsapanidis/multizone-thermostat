"""Diagnostics options: telemetry on/off, debug sensors (default off), and persistence of the options."""
import asyncio
import math
import types
import unittest
from unittest.mock import MagicMock, patch

import pytest

from mzt_replay.loader import load_prod_module
from test_config_flow_phase9_10 import _restore_ha_environment, _setup_clean_ha_environment

from custom_components.multizone_thermostat import coordinator as coord_mod
from custom_components.multizone_thermostat import telemetry as pkg_telemetry


def _load_with_real_sensor_stub():
    """Import debug_sensors and sensor against the real dummy_ha sensor stub.

    Other test modules install a meta-path finder that serves MagicMock modules, which would turn
    SensorEntity into a mock depending on collection order.
    """
    import importlib
    import importlib.util
    import os
    import sys

    pkg = "custom_components.multizone_thermostat"
    stub_path = os.path.join(os.path.dirname(__file__), "dummy_ha", "homeassistant", "components", "sensor", "__init__.py")
    spec = importlib.util.spec_from_file_location("homeassistant.components.sensor", stub_path)
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)
    names = ["homeassistant.components.sensor", f"{pkg}.debug_sensors", f"{pkg}.sensor"]
    saved = {n: sys.modules.get(n) for n in names}
    try:
        sys.modules["homeassistant.components.sensor"] = stub
        sys.modules.pop(f"{pkg}.debug_sensors", None)
        sys.modules.pop(f"{pkg}.sensor", None)
        return importlib.import_module(f"{pkg}.debug_sensors"), importlib.import_module(f"{pkg}.sensor")
    finally:
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod


ds, sensor_mod = _load_with_real_sensor_stub()

telemetry = load_prod_module("telemetry")


# ------------------------------- recorder: persist=False -------------------------------

def test_non_persistent_recorder_keeps_latest_but_writes_nothing(tmp_path):
    rec = telemetry.JsonlRecorder(str(tmp_path / "logs"), persist=False)
    rec.record("pid", "z", demand=12.0)
    rec.record("pid", "z", demand=13.0)
    assert rec.latest("z", "pid")["data"]["demand"] == 13.0
    assert rec.latest("z", "boiler") is None
    assert rec.flush() == 0
    assert not (tmp_path / "logs").exists()


def test_null_recorder_latest_is_none():
    assert telemetry.NullRecorder().latest("z", "pid") is None


# ------------------------------- coordinator wiring -------------------------------

def _fake_coordinator(tmp_path, telemetry_enabled, debug_sensors):
    hass = MagicMock()
    hass.config.path.return_value = str(tmp_path / "mzt_telemetry")
    return types.SimpleNamespace(hass=hass, telemetry_enabled=telemetry_enabled, debug_sensors=debug_sensors,
                                 _unsub_listeners=[], _telemetry=None, _async_flush_telemetry=MagicMock())


@pytest.fixture
def clean_recorder():
    pkg_telemetry.set_recorder(None)
    yield
    pkg_telemetry.set_recorder(None)


@pytest.mark.parametrize("enabled,debug,expect_recorder,expect_persist", [
    (True, False, True, True),      # default
    (False, False, False, None),    # nothing wanted
    (False, True, True, False),     # debug sensors only: in memory, nothing on disk
    (True, True, True, True),
])
def test_start_telemetry_honours_options(tmp_path, clean_recorder, enabled, debug, expect_recorder, expect_persist):
    fake = _fake_coordinator(tmp_path, enabled, debug)
    fake.zones = [{"name": "Room", "switch_entities": ["switch.valve"], "private": "do not log"}]
    fake._settings = {"weather_curve": 1.5, "private": {"token": "do not log"}}
    with patch.object(coord_mod, "async_track_time_interval", MagicMock()):
        coord_mod.MultizoneCoordinator._start_telemetry(fake)
    if not expect_recorder:
        assert fake._telemetry is None and not pkg_telemetry.get_recorder().enabled
    else:
        assert pkg_telemetry.get_recorder() is fake._telemetry
        assert fake._telemetry.persist is expect_persist
        data = fake._telemetry.latest(None, pkg_telemetry.KIND_CONFIG)["data"]
        assert data["source"] == "startup"
        assert data["zones"] == [{"name": "Room", "switch_entities": ["switch.valve"]}]
        assert data["settings"] == {"weather_curve": 1.5}
        fake.zones[0]["switch_entities"].append("switch.changed")
        assert data["zones"][0]["switch_entities"] == ["switch.valve"]
        assert "do not log" not in repr(data)


# ------------------------------- debug sensors -------------------------------

@pytest.fixture
def recorder(clean_recorder, tmp_path):
    rec = pkg_telemetry.JsonlRecorder(str(tmp_path), persist=False)
    pkg_telemetry.set_recorder(rec)
    return rec


def make_sensor(kind="pid", zone="climate.a", field="demand", label="A PID Debug"):
    return ds.TelemetryDebugSensor(MagicMock(), "entry1", MagicMock(master_device_id=None), label, zone, kind, field)


def test_sensor_without_data_reports_none_and_a_note(recorder):
    s = make_sensor()
    assert s.native_value is None
    assert s.extra_state_attributes == {"telemetry": "no record yet"}


def test_sensor_shows_latest_record_with_all_fields(recorder):
    recorder.record("pid", "climate.a", demand=42.5, p=10.0, i=2.0, d=-1.0, mode="AUTO", nested={"k": [1, 2]})
    s = make_sensor()
    assert s.native_value == 42.5
    a = s.extra_state_attributes
    assert a["p"] == 10.0 and a["mode"] == "AUTO" and a["nested"] == {"k": [1, 2]}
    assert a["record_kind"] == "pid" and a["record_zone"] == "climate.a" and a["record_seq"] == 1
    recorder.record("pid", "climate.a", demand=50.0)
    assert s.native_value == 50.0 and s.extra_state_attributes["record_seq"] == 2


def test_sensor_never_publishes_nan_or_inf(recorder):
    recorder.record("pid", "climate.a", demand=math.nan, d=math.inf, i=-math.inf)
    s = make_sensor()
    assert s.native_value == "nan"
    a = s.extra_state_attributes
    assert a["d"] == "inf" and a["i"] == "-inf"


def test_sensors_do_not_cross_zones_or_kinds(recorder):
    recorder.record("pid", "climate.a", demand=1.0)
    recorder.record("pid", "climate.b", demand=2.0)
    recorder.record("estimator", "climate.a", heating_rate=3.0)
    assert make_sensor(zone="climate.b").native_value == 2.0
    assert make_sensor(kind="estimator", field="heating_rate").native_value == 3.0


def test_sensors_are_unrecorded_diagnostic_entities():
    s = make_sensor()
    assert ds.MATCH_ALL in s._unrecorded_attributes
    assert s._attr_entity_category == "diagnostic"
    assert s._attr_should_poll is False


def test_refresh_writes_state_only_when_a_new_record_exists(recorder):
    s = make_sensor()
    s.async_write_ha_state = MagicMock()
    run = lambda: asyncio.run(s._async_refresh())  # noqa: E731
    run()                                            # nothing recorded and nothing seen yet: no write
    assert s.async_write_ha_state.call_count == 0
    recorder.record("pid", "climate.a", demand=1.0)
    run(); run()
    assert s.async_write_ha_state.call_count == 1
    recorder.record("pid", "climate.a", demand=2.0)
    run()
    assert s.async_write_ha_state.call_count == 2


def test_build_debug_sensors_covers_zones_and_boiler_with_correct_record_keys():
    sensors = ds.build_debug_sensors(MagicMock(), "e", MagicMock(master_device_id=None),
                                     [("Living", "climate.multizone_thermostat_living"), ("Bed", "climate.multizone_thermostat_bed")])
    assert len(sensors) == 2 * 3 + 1
    keys = {(s._kind, s._record_zone) for s in sensors}
    assert ("pid", "climate.multizone_thermostat_living") in keys      # keyed by climate id
    assert ("estimator", "climate.multizone_thermostat_bed") in keys
    assert ("pwm", "Living") in keys                                   # keyed by zone name
    assert ("boiler", "boiler") in keys
    assert len({s.unique_id for s in sensors}) == len(sensors)


def test_debug_sensors_are_off_by_default():
    fake_conf = {}
    assert fake_conf.get("debug_sensors", False) is False
    from custom_components.multizone_thermostat import const
    assert const.DEFAULT_DEBUG_SENSORS is False and const.DEFAULT_TELEMETRY_ENABLED is True


def test_sensor_platform_adds_debug_sensors_only_when_enabled():
    zones = [{"name": "Living"}]

    def run(debug):
        coordinator = MagicMock(zones=zones, debug_sensors=debug, master_device_id=None)
        del coordinator.plant_diagnostics
        hass = MagicMock()
        hass.data = {sensor_mod.DOMAIN: {"e": {"coordinator": coordinator}}}
        added = []
        entry = MagicMock(entry_id="e")
        with patch.dict("sys.modules", {ds.__name__: ds}):
            asyncio.run(sensor_mod.async_setup_entry(hass, entry, lambda ents: added.append(list(ents))))
        return added

    off = run(False)
    assert not any(isinstance(e, ds.TelemetryDebugSensor) for batch in off for e in batch)
    on = run(True)
    assert sum(isinstance(e, ds.TelemetryDebugSensor) for batch in on for e in batch) == 4


# ------------------------------- options flow -------------------------------

class TestDiagnosticsOptionsFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_meta, cls._saved_mods = _setup_clean_ha_environment()
        from custom_components.multizone_thermostat import const
        from custom_components.multizone_thermostat.config_flow import MultizoneOptionsFlow
        cls.const = const
        cls.Flow = MultizoneOptionsFlow

    @classmethod
    def tearDownClass(cls):
        _restore_ha_environment(cls._saved_meta, cls._saved_mods)

    def make_flow(self, data=None):
        flow = self.Flow()
        flow.hass = MagicMock()
        flow.config_entry = MagicMock()
        flow.config_entry.data = data or {self.const.CONF_BOILER_MODE: self.const.MODE_RELAY,
                                          self.const.CONF_BOILER_SWITCH: "switch.boiler",
                                          self.const.CONF_ZONES: []}
        flow.config_entry.options = {}
        flow.config_entry.update_listeners = [object()]  # avoid the reload task path
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        return flow

    def saved(self, flow):
        return flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]

    def test_menu_offers_diagnostics_and_routes_to_it(self):
        flow = self.make_flow()
        asyncio.run(flow.async_step_init(None))
        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        self.assertIn("edit_diagnostics", schema.schema[next(iter(schema.schema))].container)
        asyncio.run(flow.async_step_init({"action": "edit_diagnostics"}))
        self.assertEqual(flow.async_show_form.call_args.kwargs["step_id"], "edit_diagnostics")

    def test_defaults_are_telemetry_on_debug_sensors_off(self):
        flow = self.make_flow()
        asyncio.run(flow.async_step_init(None))
        asyncio.run(flow.async_step_edit_diagnostics(None))
        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        defaults = {str(k): k.default() for k in schema.schema}
        self.assertEqual(defaults, {"telemetry_enabled": True, "debug_sensors": False})

    def test_saving_diagnostics_persists_both_flags_in_both_boiler_modes(self):
        for mode_data in (
            None,
            {self.const.CONF_BOILER_MODE: self.const.MODE_OPENTHERM, self.const.CONF_OPENTHERM_ENTITY: "climate.ot",
             self.const.CONF_ZONES: []},
        ):
            flow = self.make_flow(mode_data)
            asyncio.run(flow.async_step_init(None))
            asyncio.run(flow.async_step_edit_diagnostics({"telemetry_enabled": False, "debug_sensors": True}))
            data = self.saved(flow)
            self.assertIs(data["telemetry_enabled"], False)
            self.assertIs(data["debug_sensors"], True)

    def test_other_option_saves_do_not_reset_diagnostics(self):
        flow = self.make_flow({self.const.CONF_BOILER_MODE: self.const.MODE_RELAY,
                               self.const.CONF_BOILER_SWITCH: "switch.boiler", self.const.CONF_ZONES: [],
                               "telemetry_enabled": False, "debug_sensors": True})
        asyncio.run(flow.async_step_init(None))
        asyncio.run(flow.async_step_edit_calendar({self.const.CONF_GLOBAL_CALENDAR: "calendar.home"}))
        data = self.saved(flow)
        self.assertIs(data["telemetry_enabled"], False)
        self.assertIs(data["debug_sensors"], True)


def test_translations_exist_for_the_diagnostics_step():
    import json
    import pathlib
    base = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "multizone_thermostat"
    for path in (base / "strings.json", base / "translations" / "en.json"):
        step = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]["edit_diagnostics"]
        assert {"telemetry_enabled", "debug_sensors"} <= step["data"].keys()
