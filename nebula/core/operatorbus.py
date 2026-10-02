from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nebula.core.engine import Engine

OPERATOR_COMMANDS_DIRNAME = "operator_commands"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_COMMAND_TTL_S = 300.0

# Actions served by the node-side operator bus (chatbot "act" mode).
ACTION_SELECT_AGGREGATOR = "select_aggregator"
ACTION_PAUSE = "pause"
ACTION_PROPOSE_DISCONNECT = "propose_disconnect"

SUPPORTED_OPERATOR_ACTIONS = (
    ACTION_SELECT_AGGREGATOR,
    ACTION_PAUSE,
    ACTION_PROPOSE_DISCONNECT,
)


def operator_commands_dir(config_dir: str | None, scenario_name: str | None) -> str | None:
    """Resolve (creating it) the operator-commands directory for a scenario.

    Returns None when the location cannot be determined (bus stays disabled).
    """
    if not config_dir or not scenario_name:
        return None
    try:
        folder = os.path.join(config_dir, scenario_name, OPERATOR_COMMANDS_DIRNAME)
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        logging.warning(f"OperatorBus | cannot create commands dir: {e}")
        return None
    else:
        return folder


def issue_operator_command(
    config_dir: str,
    scenario_name: str,
    target: str,
    action: str,
    params: dict[str, Any] | None = None,
    issued_by: str = "smac-chat",
    ttl_s: float = DEFAULT_COMMAND_TTL_S,
) -> str:
    """Write an operator command file for node(s) to pick up.

    Args:
        config_dir (str): Base config dir (e.g. ``/nebula/app/config``).
        scenario_name (str): Scenario name.
        target (str): Node idx (``"3"``) or ``"all"`` for broadcast.
        action (str): One of :data:`SUPPORTED_OPERATOR_ACTIONS`.
        params (dict): Action parameters.
        issued_by (str): Audit label.
        ttl_s (float): Seconds after which nodes ignore the command.

    Returns:
        str: Command id.

    Raises:
        ValueError: On unknown action or unresolvable directory.
    """
    if action not in SUPPORTED_OPERATOR_ACTIONS:
        msg = f"Unknown operator action: {action}"
        raise ValueError(msg)
    folder = operator_commands_dir(config_dir, scenario_name)
    if folder is None:
        msg = "Cannot resolve operator commands directory"
        raise ValueError(msg)
    command_id = uuid.uuid4().hex[:12]
    payload = {
        "id": command_id,
        "target": str(target),
        "action": action,
        "params": params or {},
        "issued_by": issued_by,
        "issued_at": time.time(),
        "ttl_s": ttl_s,
    }
    tmp_path = os.path.join(folder, f".{command_id}.tmp")
    final_path = os.path.join(folder, f"{command_id}.json")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, final_path)  # atomic publish
    logging.info(f"OperatorBus | issued {action} -> {target} (id={command_id})")
    return command_id


def read_command_result(commands_dir: str, command_id: str, node_idx: str) -> dict[str, Any] | None:
    """Read the result file of a command for a node (None when not ready)."""
    path = os.path.join(commands_dir, f"{command_id}.result.{node_idx}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


class OperatorBus:
    """Node-side poller executing operator commands from the chatbot (SMAC chat).

    The frontend writes command files into
    ``<config_dir>/<scenario>/operator_commands/`` (shared bind-mount). Each node
    polls the directory and executes the commands targeting its idx (or ``"all"``),
    writing back ``<id>.result.<idx>.json``. Commands expire after their TTL.
    """

    def __init__(self, engine: Engine, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S):
        self._engine = engine
        participant = engine.config.participant
        self._idx = str(participant["device_args"]["idx"])
        self._scenario = participant["scenario_args"].get("name")
        config_dir = os.environ.get("NEBULA_CONFIG_DIR") or participant.get("tracking_args", {}).get("config_dir", "")
        if config_dir and not config_dir.endswith("config"):
            # tracking config_dir points at the scenario folder; go one level up.
            config_dir = os.path.dirname(config_dir.rstrip("/"))
        self._commands_dir = operator_commands_dir(config_dir or None, self._scenario)
        self._poll_interval_s = poll_interval_s
        self._running = False

    @property
    def enabled(self) -> bool:
        return self._commands_dir is not None

    async def run(self):
        """Poll loop (returns immediately when the bus is disabled)."""
        if not self.enabled:
            logging.info("OperatorBus | disabled (no commands dir)")
            return
        self._running = True
        logging.info(f"OperatorBus | watching {self._commands_dir} for node {self._idx}")
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logging.warning(f"OperatorBus | poll failed: {e}")
            await asyncio.sleep(self._poll_interval_s)

    async def stop(self):
        self._running = False

    async def _poll_once(self):
        try:
            files = sorted(os.listdir(self._commands_dir))
        except FileNotFoundError:
            return
        for filename in files:
            if not filename.endswith(".json") or ".result." in filename or filename.startswith("."):
                continue
            path = os.path.join(self._commands_dir, filename)
            try:
                with open(path) as f:
                    command = json.load(f)
            except Exception as e:
                logging.debug(f"OperatorBus | skipping unreadable file {filename}: {e}")
                continue
            command_id = command.get("id", filename)
            if self._already_done(command_id):
                continue
            if not self._targets_me(command):
                continue
            if self._is_expired(command):
                self._write_result(command_id, "expired", "command TTL elapsed")
                continue
            status, detail = await self._execute(command)
            self._write_result(command_id, status, detail)

    def _targets_me(self, command: dict) -> bool:
        target = str(command.get("target", ""))
        return target in ("all", self._idx)

    def _already_done(self, command_id: str) -> bool:
        return os.path.exists(os.path.join(self._commands_dir, f"{command_id}.result.{self._idx}.json"))

    def _is_expired(self, command: dict) -> bool:
        try:
            return (time.time() - float(command.get("issued_at", 0))) > float(
                command.get("ttl_s", DEFAULT_COMMAND_TTL_S)
            )
        except (TypeError, ValueError):
            return True

    def _write_result(self, command_id: str, status: str, detail: str):
        payload = {
            "id": command_id,
            "node": self._idx,
            "status": status,
            "detail": detail,
            "finished_at": time.time(),
        }
        path = os.path.join(self._commands_dir, f"{command_id}.result.{self._idx}.json")
        try:
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logging.warning(f"OperatorBus | cannot write result: {e}")

    async def _execute(self, command: dict) -> tuple[str, str]:
        action = command.get("action")
        params = command.get("params", {}) or {}
        logging.info(f"OperatorBus | executing {action} {params} (id={command.get('id')})")
        try:
            if action == ACTION_SELECT_AGGREGATOR:
                result = await self._do_select_aggregator(params)
            elif action == ACTION_PAUSE:
                result = await self._do_pause(params)
            elif action == ACTION_PROPOSE_DISCONNECT:
                result = await self._do_propose_disconnect(params)
            else:
                result = ("error", f"unknown action: {action}")
        except Exception as e:
            logging.warning(f"OperatorBus | execution failed: {e}")
            return "error", str(e)
        else:
            return result

    async def _do_select_aggregator(self, params: dict) -> tuple[str, str]:
        from nebula.core.situationalawareness.awareness.aggregationselector.aggregationselector import (
            SUPPORTED_ALGORITHMS,
        )

        algorithm = str(params.get("algorithm", ""))
        if algorithm not in SUPPORTED_ALGORITHMS:
            return "error", f"unsupported algorithm: {algorithm}"
        engine = self._engine
        previous = engine.config.participant["aggregator_args"]["algorithm"]
        if previous == algorithm:
            return "ok", f"already using {algorithm}"
        engine.config.participant["aggregator_args"]["algorithm"] = algorithm
        from nebula.core.aggregation.aggregator import create_aggregator

        new_aggregator = create_aggregator(config=engine.config, engine=engine)
        await new_aggregator.init()
        engine._aggregator = new_aggregator
        return "ok", f"aggregator hot-swapped {previous} -> {algorithm}"

    async def _do_pause(self, params: dict) -> tuple[str, str]:
        paused = bool(params.get("paused", True))
        self._engine.operator_paused = paused
        return "ok", f"node {'paused' if paused else 'resumed'}"

    async def _do_propose_disconnect(self, params: dict) -> tuple[str, str]:
        peer = str(params.get("peer", ""))
        if not peer:
            return "error", "missing 'peer' parameter"
        try:
            sa = self._engine.sa
            negotiation = sa.sar.negotiation if sa is not None and sa.sar is not None else None
        except Exception:
            negotiation = None
        if negotiation is None or not negotiation.enabled:
            return "error", "inter-node negotiation not enabled on this node"
        proposal_id = await negotiation.propose_topology_change(
            peer=peer,
            edge_a=self._engine.config.participant["network_args"]["addr"],
            edge_b=peer,
            change="disconnect",
            reason=f"operator request via SMAC chat ({params.get('reason', '')})",
        )
        if proposal_id is None:
            return "error", f"cannot send proposal to {peer}"
        return "ok", f"proposal {proposal_id} sent to {peer}"
