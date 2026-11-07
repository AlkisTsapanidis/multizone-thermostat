from datetime import datetime
import asyncio
import inspect
import logging

from ..const import CONF_ZONE_NAME, make_zone_entity_id
from .. import telemetry

_LOGGER = logging.getLogger(__name__)

class PlantOrchestrator:
    """Orchestrates the boiler PWM ticks and demand logic."""
    
    def __init__(self, hass, demand_aggregator, boiler_backend, coordinator) -> None:
        self.hass = hass
        self.demand_aggregator = demand_aggregator
        self.boiler_backend = boiler_backend
        self.coordinator = coordinator
        
    async def async_tick(self, now: datetime) -> None:
        """Periodic PWM tick for boiler."""
        try:
            if hasattr(self.coordinator, "anti_seize_manager") and self.coordinator.anti_seize_manager.is_running:
                telemetry.get_recorder().record(
                    telemetry.KIND_PLANT, "boiler", action="anti_seize", aggregation_evaluated=False,
                )
                return
                
            await self._async_update_boiler()
        except Exception as err:
            _LOGGER.exception("Error in PlantOrchestrator.async_tick: %s", err)

    async def _async_update_boiler(self) -> None:
        """Handle the logic for pushing demand to boiler backend."""
        climate_entities = getattr(self.coordinator, "_climate_entities", None)
        if isinstance(climate_entities, dict):
            for ent in list(climate_entities.values()):
                if hasattr(ent, "check_sensor_staleness"):
                    ent.check_sensor_staleness()

        frost_emergency = False
        frost_mgr = getattr(self.coordinator, "frost_manager", None)
        handle_fn = getattr(frost_mgr, "async_handle_frost_emergencies", None)
        if inspect.iscoroutinefunction(handle_fn):
            frost_emergency = bool(await handle_fn())

        if not frost_emergency:
            for climate_id in list(getattr(self.coordinator, "_climate_callbacks", {}).keys()):
                if hasattr(self.coordinator, "is_zone_in_frost_emergency") and self.coordinator.is_zone_in_frost_emergency(climate_id):
                    frost_emergency = True
                    break

        # Hard stop if master switch is OFF
        if not self.coordinator.get_master_state() and not frost_emergency:
            telemetry.get_recorder().record(
                telemetry.KIND_PLANT, "boiler", action="master_off", frost_emergency=False,
                aggregation_evaluated=False,
            )
            await self.boiler_backend.async_emergency_off()
            return

        recorder = telemetry.get_recorder()
        raw_demands = None
        if recorder.enabled:
            try:
                get_demands = getattr(self.demand_aggregator, "get_all_demands", None)
                if callable(get_demands):
                    demands = get_demands()
                    if isinstance(demands, dict):
                        raw_demands = demands.copy()
            except Exception:
                _LOGGER.exception("Unable to capture pre-suppression plant demands")
        suppressed = {}
        plant_diag = getattr(self.coordinator, "plant_diagnostics", None)
        zones = getattr(self.coordinator, "zones", None)
        if plant_diag and isinstance(zones, list):
            for zone in zones:
                zone_name = zone.get(CONF_ZONE_NAME) if isinstance(zone, dict) else None
                if zone_name:
                    cid = make_zone_entity_id(zone_name)
                    if hasattr(plant_diag, "evaluate_zone_anomalies"):
                        plant_diag.evaluate_zone_anomalies(cid)
                    if not frost_emergency and hasattr(plant_diag, "has_critical_sensor_fault"):
                        if plant_diag.has_critical_sensor_fault(cid) is True:
                            suppressed[cid] = "critical_sensor_fault"
                            self.demand_aggregator.set_zone_demand(cid, 0.0)

        if not frost_emergency and isinstance(climate_entities, dict):
            for climate_id, ent in list(climate_entities.items()):
                if getattr(ent, "current_temperature", False) is None:
                    suppressed[climate_id] = "missing_temperature"
                    self.demand_aggregator.set_zone_demand(climate_id, 0.0)
                elif (
                    plant_diag
                    and hasattr(plant_diag, "has_critical_sensor_fault")
                    and plant_diag.has_critical_sensor_fault(climate_id) is True
                ):
                    suppressed[climate_id] = "critical_sensor_fault"
                    self.demand_aggregator.set_zone_demand(climate_id, 0.0)

        dhw_active = getattr(self.coordinator, "is_dhw_active", False)
        agg = self.demand_aggregator.calculate(
            is_dhw_active=dhw_active,
            frost_emergency=frost_emergency,
        )
        if recorder.enabled:
            try:
                primary = getattr(agg, "primary_demands", None)
                secondary = getattr(agg, "secondary_demands", None)
                recorder.record(
                    telemetry.KIND_PLANT, "boiler", action="aggregate", aggregation_evaluated=True,
                    raw_demands=raw_demands, suppressed=suppressed,
                    frost_emergency=frost_emergency, dhw_active=dhw_active,
                    demand=agg.demand, peak_zone=agg.peak_zone, strategy=getattr(agg, "strategy", None),
                    primary_demands=primary.copy() if isinstance(primary, dict) else None,
                    secondary_demands=secondary.copy() if isinstance(secondary, dict) else None,
                    active_primary_count=getattr(agg, "active_primary_count", None),
                    active_secondary_count=getattr(agg, "active_secondary_count", None),
                    result_frost_emergency=getattr(agg, "frost_emergency", None),
                    result_dhw_active=getattr(agg, "dhw_active", None),
                )
            except Exception:
                _LOGGER.exception("Unable to capture plant aggregation telemetry")
                
        await self.boiler_backend.async_update(agg.demand, agg.peak_zone, frost_emergency)
