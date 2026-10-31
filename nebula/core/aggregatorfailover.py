from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import zlib

from nebula.addons.functions import print_msg_box
from nebula.config.defaults import (
    AFAS_DEGRADE_CONFIRM_S,
    AFAS_ELECTION_COOLDOWN_S,
    AFAS_ENABLED,
    AFAS_MAX_BUFFER_TRANSFER_MB,
    AFAS_SWITCH_HOLD_ROUNDS,
)
from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import UpdateReceivedEvent
from nebula.core.network.networkmonitor import PARTICIPANT_ROLES, NetworkMonitor
from nebula.core.noderole import Role
from nebula.core.situationalawareness.awareness.cognitive.decisionlog import CognitiveDecisionLog


class AggregatorFailoverManager:
    """AFAS coordinator: elects and hot-swaps aggregators using network metrics only."""

    def __init__(self, engine, monitor: NetworkMonitor):
        self.engine = engine
        self.monitor = monitor
        participant = engine.config.participant
        args = participant.get("network_monitor_args") or {}
        self.enabled = bool(args.get("enabled", AFAS_ENABLED))
        self.cooldown_s = float(args.get("election_cooldown_s", AFAS_ELECTION_COOLDOWN_S))
        self.degrade_confirm_s = float(args.get("degrade_confirm_s", AFAS_DEGRADE_CONFIRM_S))
        self.switch_hold_rounds = int(args.get("switch_hold_rounds", AFAS_SWITCH_HOLD_ROUNDS))
        self.max_buffer_transfer_bytes = int(args.get("max_buffer_transfer_mb", AFAS_MAX_BUFFER_TRANSFER_MB)) * 1024 * 1024

        federation = participant.get("scenario_args", {}).get("federation", "DFL")
        self.federation = federation
        self.architecture_label = "Centralizada" if federation == "CFL" else "Semi-descentralizada"

        self.current_aggregator = self._initial_aggregator()
        self.term = 0
        self._task: asyncio.Task | None = None
        self._last_switch = 0.0
        self._bad_aggregator: str | None = None
        self._bad_since = 0.0
        self._seen_announces: set[tuple[int, str]] = set()
        self._announced_peers: set[str] = set()
        self._last_announce: dict | None = None
        self._alerted: set[tuple[int, str]] = set()
        self._transferred: dict | None = None
        self._last_connectivity_check = 0.0

        log_dir = participant.get("tracking_args", {}).get("log_dir")
        scenario_name = participant.get("scenario_args", {}).get("name")
        self._decision_log = CognitiveDecisionLog(log_dir=log_dir, scenario_name=scenario_name)

    def _initial_aggregator(self) -> str | None:
        role = self.engine.rb.get_role_name(True)
        if role in ("aggregator", "server"):
            return self.engine.addr

        candidates = self._aggregator_candidates()
        if not candidates:
            candidates = [
                (False, node_id, node["addr"])
                for node_id, node in self.monitor.node_table.items()
                if node.get("start")
            ]
        if not candidates:
            return None
        return sorted(candidates)[0][2]

    def _aggregator_candidates(self) -> list[tuple[bool, str, str]]:
        candidates = []
        wanted = "server" if self.federation == "CFL" else "aggregator"
        for node_id, node in self.monitor.node_table.items():
            if node.get("role") == wanted:
                candidates.append((not node.get("start", False), node_id, node["addr"]))
        if not candidates and self.federation == "CFL":
            for node_id, node in self.monitor.node_table.items():
                if node.get("role") in ("aggregator", "trainer_aggregator"):
                    candidates.append((not node.get("start", False), node_id, node["addr"]))
        return candidates

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        await self.monitor.start()
        self._task = asyncio.create_task(self._run())
        logging.info(
            f"AFAS | started | architecture={self.architecture_label} | initial aggregator={self.current_aggregator}"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.monitor.stop()

    async def _run(self) -> None:
        while True:
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("AFAS | failover loop error")
            await asyncio.sleep(1.0)

    def _resolve_current_aggregator(self) -> str | None:
        if self.federation == "SDFL":
            source = getattr(self.engine, "_global_model_source", None)
            if source and (self.term == 0 or not self.current_aggregator):
                self.current_aggregator = source
        if (
            self.engine.rb.get_role_name(True) in ("aggregator", "server")
            and (self.term == 0 or not self.current_aggregator)
        ):
            self.current_aggregator = self.engine.addr
        if not self.current_aggregator:
            candidates = self._aggregator_candidates()
            if candidates:
                self.current_aggregator = sorted(candidates)[0][2]
            else:
                connections = set(getattr(self.engine.cm, "connections", {}))
                connections.discard(self.engine.addr)
                if len(connections) == 1:
                    self.current_aggregator = next(iter(connections))
        return self.current_aggregator

    async def evaluate(self) -> None:
        if not self.enabled or self.engine.round is None:
            return
        if time.time() - self.monitor.started_at < self.monitor.startup_grace_s:
            return

        current = self._resolve_current_aggregator()
        if not current:
            return
        if current == self.engine.addr:
            self._clear_bad()
            await self._ensure_connectivity()
            return

        reasons = self.monitor.degraded_reasons(current)
        if not reasons:
            self._clear_bad()
            return

        now = time.time()
        if self._bad_aggregator != current:
            self._bad_aggregator = current
            self._bad_since = now
            logging.warning(f"AFAS | aggregator {current} degrading: {' | '.join(reasons)}")
            return
        if now - self._bad_since < self.degrade_confirm_s:
            return
        if now - self._last_switch < self.cooldown_s:
            return

        new_aggregator = self._elect(current)
        if not new_aggregator or new_aggregator == current:
            logging.warning(f"AFAS | no suitable replacement for {current}")
            return
        await self._execute_switch(current, new_aggregator, reasons)

    def _clear_bad(self) -> None:
        self._bad_aggregator = None
        self._bad_since = 0.0

    def _candidates(self, exclude: str) -> set[str]:
        candidates = set()
        for node in self.monitor.node_table.values():
            addr = node.get("addr")
            role = node.get("role")
            if not addr or addr == exclude:
                continue
            if role and role not in PARTICIPANT_ROLES:
                continue
            candidates.add(addr)

        own_role = self.engine.rb.get_role_name(True)
        if self.engine.addr != exclude and (not own_role or own_role in PARTICIPANT_ROLES):
            candidates.add(self.engine.addr)

        self_id = self.monitor.addr_to_id.get(self.engine.addr)
        self_node = self.monitor.nodes.get(self_id, {}) if self_id else {}
        if not self_node.get("neighbors"):
            candidates |= set(getattr(self.engine.cm, "connections", {}))
        return candidates

    def _elect(self, failed_aggregator: str) -> str | None:
        candidates = self._candidates(failed_aggregator)
        if not candidates:
            return None
        if self.federation == "CFL":
            return self.monitor.best_candidate(candidates)

        cluster = self.monitor.cluster_members(self.engine.addr)
        intra = candidates & cluster
        inter = candidates - cluster
        pools = [intra, inter] if failed_aggregator in cluster else [inter, intra]
        for pool in pools:
            if not pool:
                continue
            best = self.monitor.best_candidate(pool)
            if best:
                return best
        return None

    async def _execute_switch(self, old: str, new: str, reasons: list[str]) -> None:
        term = max(int(time.time() * 1000), self.term + 1)
        cause = " | ".join(reasons)
        self.term = term
        self.current_aggregator = new
        self._last_switch = time.time()
        self._seen_announces.add((term, new))
        self._clear_bad()
        self._hold_leadership()

        logging.warning(f"AFAS | switching aggregator {old} -> {new} | term={term} | {cause}")
        if old == self.engine.addr:
            await self._ensure_connectivity(force=True)
            await self._send_buffer(new, term)
        if new == self.engine.addr:
            await self._ensure_connectivity(force=True)
        await self._announce(old, new, term, cause)

        if new == self.engine.addr:
            await self._apply_role(Role.AGGREGATOR)
        elif old == self.engine.addr:
            await self._apply_role(Role.TRAINER)

        if new == self.engine.addr or old == self.engine.addr:
            await self._alert(old, new, cause, term)

    async def _announce(self, old: str, new: str, term: int, cause: str) -> None:
        payload = {
            "afas": "announce",
            "term": term,
            "old": old,
            "new": new,
            "cause": cause,
            "architecture": self.architecture_label,
        }
        recipients = set(getattr(self.engine.cm, "connections", {}))
        if new == self.engine.addr:
            recipients |= self._candidates(old)
        recipients.discard(self.engine.addr)
        self._last_announce = payload
        self._announced_peers |= recipients & set(getattr(self.engine.cm, "connections", {}))
        for addr in recipients:
            await self._send_control("recovery", payload, addr)

    async def _send_control(self, action: str, payload: dict, dest: str) -> None:
        try:
            message = self.engine.cm.create_message(
                "control",
                action,
                log=json.dumps(payload, separators=(",", ":")),
            )
            await self.engine.cm.send_message(dest, message)
        except Exception as e:
            logging.debug(f"AFAS | cannot send {payload.get('afas')} to {dest}: {e}")

    async def handle_control(self, source: str, message) -> None:
        try:
            payload = json.loads(getattr(message, "log", "") or "{}")
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("afas") != "announce":
            return
        await self._handle_announce(source, payload)

    async def _handle_announce(self, source: str, payload: dict) -> None:
        try:
            term = int(payload.get("term", 0))
        except (TypeError, ValueError):
            return
        new = payload.get("new")
        old = payload.get("old")
        cause = str(payload.get("cause", "degradación de red"))
        if not new:
            return
        if term < self.term:
            return
        if term == self.term and new == self.current_aggregator:
            return
        if term == self.term and new > (self.current_aggregator or ""):
            return

        self.term = term
        previous = self.current_aggregator
        self.current_aggregator = new
        self._last_switch = max(self._last_switch, time.time())
        self._hold_leadership()

        await self._apply_announced_roles(new, old)
        await self._forward_announce(source, payload, term, old, new, cause)
        if new == self.engine.addr or old == self.engine.addr:
            await self._alert(old or previous or "desconocido", new, cause, term)

    async def _apply_announced_roles(self, new: str, old: str | None) -> None:
        if new == self.engine.addr:
            await self._ensure_connectivity(force=True)
            await self._apply_role(Role.AGGREGATOR)
        elif self.engine.rb.get_role_name(True) in ("aggregator", "server") or old == self.engine.addr:
            if old == self.engine.addr:
                await self._ensure_connectivity(force=True)
            await self._apply_role(Role.TRAINER)

    async def _forward_announce(self, source: str, payload: dict, term: int, old, new: str, cause: str) -> None:
        if (term, new) in self._seen_announces:
            return
        self._seen_announces.add((term, new))
        forwarding = {
            "afas": "announce",
            "term": term,
            "old": old,
            "new": new,
            "cause": cause,
            "architecture": payload.get("architecture", self.architecture_label),
        }
        for addr in set(getattr(self.engine.cm, "connections", {})) - {self.engine.addr, source}:
            await self._send_control("recovery", forwarding, addr)

    async def _apply_role(self, role: Role) -> None:
        if self.engine.rb.get_role() == role:
            return
        lock = self.engine._round_in_process_lock
        if await lock.locked_async():
            logging.info(f"AFAS | round in progress; role {role.value} will apply at the next round")
            await self.engine.rb.set_next_role(role)
            return

        lock_task = asyncio.create_task(lock.acquire_async())
        try:
            await asyncio.wait_for(lock_task, timeout=3)
            await self.engine.rb.set_next_role(role)
            await self.engine.update_self_role()
            with contextlib.suppress(Exception):
                self.engine.aggregator.us._role = role.value
            logging.info(f"AFAS | local role updated to {role.value}")
        except TimeoutError:
            logging.info(f"AFAS | role lock busy; role {role.value} will apply at the next round")
            await self.engine.rb.set_next_role(role)
        finally:
            if lock_task.done() and not lock_task.cancelled():
                with contextlib.suppress(Exception):
                    lock_task.result()
            if await lock.locked_async():
                with contextlib.suppress(RuntimeError):
                    await lock.release_async()

    def _hold_leadership(self) -> None:
        current_round = self.engine.round or 0
        self.engine._afas_leadership_hold_until = current_round + self.switch_hold_rounds

    async def _ensure_connectivity(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_connectivity_check < 5.0:
            return
        self._last_connectivity_check = now
        current = self.current_aggregator
        if self.federation == "CFL":
            targets = set(self.monitor.addr_to_id)
        else:
            targets = self.monitor.cluster_members(self.engine.addr)
            if current:
                targets.add(current)
        targets.discard(self.engine.addr)
        connections = getattr(self.engine.cm, "connections", {})
        attempted = False
        for addr in targets:
            if addr not in connections:
                attempted = True
                try:
                    await self.engine.cm.connect(addr, direct=True)
                except Exception as e:
                    logging.debug(f"AFAS | cannot connect to {addr}: {e}")
        if attempted:
            await asyncio.sleep(0.3)
        if self.current_aggregator == self.engine.addr and self._last_announce:
            for addr in targets - self._announced_peers:
                self._announced_peers.add(addr)
                await self._send_control("recovery", self._last_announce, addr)

    async def _send_buffer(self, new_aggregator: str, term: int) -> None:
        try:
            updates = await self.engine.aggregator.us.get_round_updates()
        except Exception as e:
            logging.warning(f"AFAS | cannot read aggregation buffer: {e}")
            return
        if not updates:
            return

        sources = {}
        for source, (model, weight) in updates.items():
            try:
                serialized = self.engine.trainer.serialize_model(model)
                encoded = base64.b64encode(zlib.compress(serialized)).decode("ascii")
                sources[source] = {"model": encoded, "weight": float(weight or 1.0)}
            except Exception as e:
                logging.warning(f"AFAS | cannot serialize buffered update from {source}: {e}")

        payload = {
            "afas": "buffer",
            "term": term,
            "old": self.engine.addr,
            "round": self.engine.round,
            "sources": sources,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        if len(raw) > self.max_buffer_transfer_bytes:
            logging.warning(
                f"AFAS | aggregation buffer too large ({len(raw)} bytes > {self.max_buffer_transfer_bytes}); "
                "skipping state transfer"
            )
            return

        if new_aggregator not in getattr(self.engine.cm, "connections", {}):
            with contextlib.suppress(Exception):
                await self.engine.cm.connect(new_aggregator, direct=True)
                await asyncio.sleep(0.5)
        if new_aggregator not in getattr(self.engine.cm, "connections", {}):
            logging.warning(f"AFAS | cannot transfer buffer to {new_aggregator}: no connection")
            return
        try:
            message = self.engine.cm.create_message(
                "model",
                round=-2,
                parameters=zlib.compress(raw),
                weight=1,
            )
            await self.engine.cm.send_message(new_aggregator, message, "model")
            logging.info(
                f"AFAS | aggregation buffer sent to {new_aggregator} "
                f"({len(sources)} updates, {len(raw)} bytes)"
            )
        except Exception as e:
            logging.warning(f"AFAS | cannot send aggregation buffer to {new_aggregator}: {e}")

    async def handle_buffer_transfer(self, source: str, message) -> None:
        parameters = getattr(message, "parameters", b"") or b""
        if len(parameters) > self.max_buffer_transfer_bytes:
            logging.warning(f"AFAS | buffer transfer from {source} exceeds size limit; ignoring")
            return
        try:
            payload = json.loads(zlib.decompress(parameters))
        except Exception as e:
            logging.warning(f"AFAS | invalid buffer transfer from {source}: {e}")
            return
        if not isinstance(payload, dict) or payload.get("afas") != "buffer":
            return
        if payload.get("old") not in (None, source):
            logging.warning(f"AFAS | ignoring buffer from {source} claiming origin {payload.get('old')}")
            return
        try:
            term = int(payload.get("term", 0))
        except (TypeError, ValueError):
            return
        if term < self.term:
            return

        updates = {}
        for update_source, entry in (payload.get("sources") or {}).items():
            try:
                serialized = zlib.decompress(base64.b64decode(entry["model"]))
                updates[update_source] = (
                    self.engine.trainer.deserialize_model(serialized),
                    float(entry.get("weight", 1.0)),
                )
            except Exception as e:
                logging.warning(f"AFAS | cannot decode buffered update from {update_source}: {e}")

        if updates:
            self._transferred = {"round": payload.get("round"), "term": term, "updates": updates}
            logging.info(f"AFAS | received aggregation buffer from {source} with {len(updates)} updates")

    async def replay_transferred_updates(self) -> None:
        transferred = self._transferred
        self._transferred = None
        if not transferred or self.engine.round is None:
            return
        if self.term and transferred.get("term") != self.term:
            logging.info("AFAS | discarding buffer from a stale term")
            return
        try:
            transferred_round = int(transferred.get("round"))
        except (TypeError, ValueError):
            return
        if abs(self.engine.round - transferred_round) > 1:
            logging.info(
                f"AFAS | discarding stale buffer from round {transferred_round} (current {self.engine.round})"
            )
            return
        for source, (model, weight) in transferred["updates"].items():
            event = UpdateReceivedEvent(model, weight, source, self.engine.round)
            await EventManager.get_instance().publish_node_event(event)
        logging.info(f"AFAS | replayed {len(transferred['updates'])} buffered updates into round {self.engine.round}")

    async def _alert(self, old: str, new: str, cause: str, term: int) -> None:
        key = (term, new)
        if key in self._alerted:
            return
        self._alerted.add(key)
        text = (
            "[AFAS ALERT] 🔄 Cambio Dinámico de Agregador Ejecutado\n"
            f"• Arquitectura: {self.architecture_label}\n"
            f"• Agregador Anterior: {self.monitor.node_label(old)}\n"
            f"• Nuevo Agregador: {self.monitor.node_label(new)}\n"
            f"• Causa (Métrica de Red): {cause}\n"
            "• Estado del Entrenamiento: Continuo y activo (sin pausa)."
        )
        print(text, flush=True)
        print_msg_box(msg=text, indent=2, title="AFAS")
        logging.warning(text)
        await self._decision_log.record(
            owner="AFAS",
            action="select_aggregator",
            command_type="network_failover",
            priority=100,
            confidence=1.0,
            explanation=text,
            round_number=self.engine.round,
            extra={
                "old_aggregator": old,
                "new_aggregator": new,
                "term": term,
                "architecture": self.architecture_label,
                "cause": cause,
                "metrics_only": True,
            },
        )
