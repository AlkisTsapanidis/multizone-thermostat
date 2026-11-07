"""Reconstruct selected recorded control inputs without simulating Home Assistant."""
from copy import deepcopy
from math import isnan


def _values_equal(left, right):
    if isinstance(left, float) and isinstance(right, float) and isnan(left) and isnan(right):
        return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_values_equal(a, b) for a, b in zip(left, right))
    return left == right


def reconstruct_configured_inputs(records):
    """Reconstruct monitored HA states from complete initial and contiguous change records."""
    manifest = None
    states = {}
    epoch = None
    step = 0
    ready = False
    sequence = 0
    history = []
    seen_epochs = set()
    for rec in records:
        data = rec.get("data", {})
        event = data.get("event")
        is_state = rec.get("kind") == "sensor" and data.get("source") == "configured_input"
        if not is_state and not (rec.get("kind") == "event" and event in (
            "configured_inputs_start", "configured_inputs_ready",
        )):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= sequence:
            raise ValueError("Configured input records have invalid ordering")
        sequence = seq
        if event == "configured_inputs_start":
            entities, new_epoch = data.get("entities"), data.get("epoch")
            if (manifest is not None and not ready) or not isinstance(new_epoch, str) or not new_epoch:
                raise ValueError("Configured input initialization is incomplete")
            if new_epoch in seen_epochs or not isinstance(entities, list) or not entities or any(
                not isinstance(entity, str) or not entity for entity in entities
            ) or len(set(entities)) != len(entities):
                raise ValueError("Configured input manifest is invalid")
            manifest, epoch, states, step, ready = set(entities), new_epoch, {}, 0, False
            seen_epochs.add(epoch)
            continue
        if manifest is None or data.get("epoch") != epoch:
            raise ValueError("Configured input record lacks its manifest")
        if event == "configured_inputs_ready":
            if ready or data.get("next_step") != step or set(states) != manifest:
                raise ValueError("Configured input initial snapshot is incomplete")
            ready = True
            continue
        entity = rec.get("zone")
        if (not isinstance(data.get("step"), int) or isinstance(data["step"], bool)
                or data["step"] != step or entity not in manifest):
            raise ValueError("Configured input history has a missing step or unknown entity")
        step += 1
        snapshot = {key: deepcopy(data.get(key)) for key in (
            "present", "state", "attributes", "last_changed", "last_updated",
        )}
        if (any(key not in data for key in snapshot) or not isinstance(snapshot["present"], bool)
                or not isinstance(snapshot["attributes"], dict)):
            raise ValueError("Configured input state is incomplete")
        observation = data.get("observation")
        if observation == "initial":
            if ready or entity in states:
                raise ValueError("Configured input initial snapshot is duplicated or late")
        elif observation == "change":
            if not ready or entity not in states or "old" not in data or not _values_equal(data["old"], states[entity]):
                raise ValueError("Configured input previous state diverged or is missing")
        else:
            raise ValueError("Configured input observation is invalid")
        states[entity] = snapshot
        history.append({"seq": seq, "epoch": epoch, "step": data["step"], "entity": entity,
                        "observation": observation, "event_time": data.get("event_time"),
                        "state": deepcopy(snapshot)})
    if manifest is None or not ready:
        raise ValueError("Configured input history lacks a complete initial snapshot")
    return history


def reconstruct_persistent_settings(records):
    """Reconstruct recorded setting mutations, including failed saves and unknown values."""
    epoch = None
    step = 0
    sequence = 0
    state = {}
    pending = {}
    history = []
    seen_epochs = set()
    for rec in records:
        data = rec.get("data", {})
        source = data.get("source")
        if rec.get("kind") != "config" or source not in (
            "startup", "persistent_setting_mutation", "persistent_setting_direct", "persistent_setting_save",
        ):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= sequence:
            raise ValueError("Setting history has invalid ordering")
        sequence = seq
        if source == "startup":
            new_epoch, next_step = data.get("settings_epoch"), data.get("settings_next_step")
            if (pending or not isinstance(data.get("settings"), dict)
                    or not isinstance(new_epoch, str) or not new_epoch or new_epoch in seen_epochs
                    or not isinstance(next_step, int) or isinstance(next_step, bool) or next_step < 0):
                raise ValueError("Setting checkpoint is incomplete")
            epoch, step = new_epoch, next_step
            seen_epochs.add(epoch)
            state = {key: {"value_recorded": True, "value": deepcopy(value)}
                     for key, value in data["settings"].items()}
            history.append({"seq": seq, "kind": "checkpoint", "state": deepcopy(state)})
            continue
        if epoch is None or data.get("epoch") != epoch:
            raise ValueError("Setting history lacks its checkpoint")
        index = data.get("step")
        if source in ("persistent_setting_mutation", "persistent_setting_direct"):
            key, previous, current = data.get("key"), data.get("previous"), data.get("current")
            if not isinstance(index, int) or isinstance(index, bool) or index != step or not isinstance(key, str) or not isinstance(
                data.get("previous_present"), bool
            ) or any(not isinstance(value, dict) or not isinstance(value.get("value_recorded"), bool)
                     or (value["value_recorded"] and "value" not in value)
                     for value in (previous, current)):
                raise ValueError("Setting mutation is incomplete or has a missing step")
            if key in state and (not data["previous_present"] or not _values_equal(state[key], previous)):
                raise ValueError("Setting previous value diverged from history")
            state[key] = deepcopy(current)
            item = {"seq": seq, "kind": "mutation", "key": key, "step": index,
                    "current": deepcopy(current), "state": deepcopy(state), "save": None}
            if source == "persistent_setting_mutation":
                pending[index] = item
            else:
                item["save"] = {"scheduled_separately": True}
            history.append(item)
            step += 1
        else:
            if not isinstance(index, int) or isinstance(index, bool) or index not in pending or not isinstance(data.get("completed"), bool) or not isinstance(
                data.get("cancelled"), bool
            ) or "error" not in data:
                raise ValueError("Setting save outcome lacks its mutation")
            if ((data["completed"] and (data["cancelled"] or data["error"] is not None))
                    or (data["cancelled"] and data["error"] is not None)
                    or (not data["completed"] and not data["cancelled"] and not isinstance(data["error"], str))):
                raise ValueError("Setting save outcome is inconsistent")
            pending.pop(index)["save"] = {"completed": data["completed"], "cancelled": data["cancelled"],
                                           "error": data["error"]}
    if epoch is None or pending:
        raise ValueError("Setting history lacks checkpoint or save outcome")
    return history


def reconstruct_preset_history(records):
    """Apply recorded preset mutations to a complete control-state checkpoint."""
    sequence = 0
    epoch = None
    step = 0
    state = None
    history = []
    seen_epochs = set()
    for rec in records:
        data = rec.get("data", {})
        source = data.get("source")
        if rec.get("kind") != "config" or source not in ("preset_checkpoint", "preset_change"):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= sequence:
            raise ValueError("Preset history has invalid ordering")
        sequence = seq
        if source == "preset_checkpoint":
            epoch, step = data.get("epoch"), data.get("next_step")
            if (not isinstance(epoch, str) or not epoch or epoch in seen_epochs
                    or not isinstance(step, int) or isinstance(step, bool) or step < 0
                    or data.get("complete") is not True or not isinstance(data.get("presets"), dict)
                    or not isinstance(data.get("zone_modes"), dict)
                    or not isinstance(data.get("pre_window_state"), dict)
                    or not isinstance(data.get("current_preset"), str)):
                raise ValueError("Preset checkpoint is incomplete")
            seen_epochs.add(epoch)
            state = {"presets": deepcopy(data["presets"]), "current_preset": data["current_preset"],
                     "zone_modes": deepcopy(data["zone_modes"]),
                     "pre_window_state": deepcopy(data["pre_window_state"])}
            history.append({"seq": seq, "kind": "checkpoint", "state": deepcopy(state)})
            continue
        if state is None or data.get("epoch") != epoch or data.get("step") != step:
            raise ValueError("Preset history lacks a checkpoint or has a missing step")
        action, value = data.get("action"), data.get("value")
        if action == "current" and isinstance(value, str):
            state["current_preset"] = value
        elif action in ("zone_mode", "window_store", "window_clear") and isinstance(
            data.get("climate_id"), str
        ) and isinstance(value, str):
            climate_id = data["climate_id"]
            if action == "zone_mode":
                state["zone_modes"][climate_id] = value
            elif action == "window_store":
                if climate_id in state["pre_window_state"]:
                    raise ValueError("Window override was already active")
                state["pre_window_state"][climate_id] = value
            else:
                if state["pre_window_state"].get(climate_id) != value:
                    raise ValueError("Window override state diverged")
                del state["pre_window_state"][climate_id]
        elif action in ("mode", "target_temp") and isinstance(data.get("preset"), str) and isinstance(
            data.get("climate_id"), str
        ) and (value is None or isinstance(value, (str, bool, int, float))):
            state["presets"].setdefault(data["preset"], {}).setdefault(data["climate_id"], {})[action] = value
        else:
            raise ValueError("Preset mutation is invalid")
        history.append({"seq": seq, "kind": "mutation", "step": step, "state": deepcopy(state)})
        step += 1
    if state is None:
        raise ValueError("Preset history lacks a checkpoint")
    return history


def reconstruct_runtime_control_settings(records):
    """Reconstruct runtime coordinator settings, independently of HA entity persistence."""
    expected_keys = {"min_cycle_on", "min_cycle_off", "valve_opening_delay",
                     "derivative_window_min", "derivative_limit_pct", "master_enabled"}
    epoch = None
    step = 0
    sequence = 0
    state = None
    seen_epochs = set()
    history = []
    for rec in records:
        data = rec.get("data", {})
        source = data.get("source")
        if rec.get("kind") != "config" or source not in ("startup", "runtime_control_setting"):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= sequence:
            raise ValueError("Runtime setting history has invalid ordering")
        sequence = seq
        if source == "startup":
            epoch, step, state = data.get("runtime_epoch"), data.get("runtime_next_step"), data.get("runtime_settings")
            if (not isinstance(epoch, str) or not epoch or epoch in seen_epochs
                    or not isinstance(step, int) or isinstance(step, bool) or step < 0
                    or not isinstance(state, dict) or set(state) != expected_keys):
                raise ValueError("Runtime setting checkpoint is incomplete")
            seen_epochs.add(epoch)
            state = deepcopy(state)
            history.append({"seq": seq, "kind": "checkpoint", "state": deepcopy(state)})
            continue
        index, key = data.get("step"), data.get("key")
        if (state is None or data.get("epoch") != epoch or not isinstance(index, int)
                or isinstance(index, bool) or index != step or key not in expected_keys
                or "previous" not in data or "value" not in data):
            raise ValueError("Runtime setting history has a missing or invalid mutation")
        if not _values_equal(state[key], data["previous"]):
            raise ValueError("Runtime setting previous value diverged")
        state[key] = data["value"]
        history.append({"seq": seq, "kind": "mutation", "step": step, "key": key, "state": deepcopy(state)})
        step += 1
    if state is None:
        raise ValueError("Runtime setting history lacks a checkpoint")
    return history
