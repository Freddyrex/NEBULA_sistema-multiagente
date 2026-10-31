from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nebula.config.defaults import (
    AFAS_DIGEST_INTERVAL_S,
    AFAS_ENABLED,
    AFAS_FAILED_PROBES_THRESHOLD,
    AFAS_HISTORY_SIZE,
    AFAS_JITTER_THRESHOLD_MS,
    AFAS_LOSS_THRESHOLD_PCT,
    AFAS_METRICS_TTL_S,
    AFAS_PROBE_INTERVAL_S,
    AFAS_PROBE_TIMEOUT_S,
    AFAS_RTT_THRESHOLD_MS,
    AFAS_STARTUP_GRACE_S,
)

if TYPE_CHECKING:
    from nebula.core.engine import Engine

AFAS_PROBE = "afas_probe"
AFAS_PONG = "afas_pong"
AFAS_METRICS = "afas_metrics"

PARTICIPANT_ROLES = {"trainer", "trainer_aggregator", "aggregator", "server"}


@dataclass
class PeerMetrics:
    """Network metrics observed for a single peer."""

    addr: str
    rtt_ms: float | None = None
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    consecutive_failures: int = 0
    last_seen: float = 0.0
    reachable: bool = False
    source: str = "local"

    def as_dict(self, now: float | None = None) -> dict:
        return {
            "addr": self.addr,
            "rtt_ms": self.rtt_ms,
            "jitter_ms": self.jitter_ms,
            "loss_pct": self.loss_pct,
            "consecutive_failures": self.consecutive_failures,
            "last_seen": self.last_seen,
            "reachable": self.reachable,
            "source": self.source,
            "ts": now if now is not None else time.time(),
        }


class NetworkMonitor:
    """Lightweight network probe/gossip service used by AFAS.

    Only network signals are collected: RTT, jitter, packet loss and timeouts.
    Model quality, CPU and memory are deliberately not observed here.
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        participant = engine.config.participant
        args = participant.get("network_monitor_args") or {}
        self.enabled = bool(args.get("enabled", AFAS_ENABLED))
        self.probe_interval_s = float(args.get("probe_interval_s", AFAS_PROBE_INTERVAL_S))
        self.probe_timeout_s = float(args.get("probe_timeout_s", AFAS_PROBE_TIMEOUT_S))
        self.rtt_threshold_ms = float(args.get("rtt_threshold_ms", AFAS_RTT_THRESHOLD_MS))
        self.jitter_threshold_ms = float(args.get("jitter_threshold_ms", AFAS_JITTER_THRESHOLD_MS))
        self.loss_threshold_pct = float(args.get("loss_threshold_pct", AFAS_LOSS_THRESHOLD_PCT))
        self.failed_probes_threshold = int(args.get("failed_probes_threshold", AFAS_FAILED_PROBES_THRESHOLD))
        self.history_size = int(args.get("history_size", AFAS_HISTORY_SIZE))
        self.metrics_ttl_s = float(args.get("metrics_ttl_s", AFAS_METRICS_TTL_S))
        self.startup_grace_s = float(args.get("startup_grace_s", AFAS_STARTUP_GRACE_S))
        self.digest_interval_s = float(args.get("digest_interval_s", AFAS_DIGEST_INTERVAL_S))

        self.own_addr = engine.addr
        self.federation = participant.get("scenario_args", {}).get("federation", "DFL")
        self._task: asyncio.Task | None = None
        self._started_at = time.time()
        self._seq = 0
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.history_size))
        self._outstanding: dict[int, tuple[str, float]] = {}
        self._global: dict[str, PeerMetrics] = {}
        self._known_connected: set[str] = set()
        self._last_digest = 0.0
        self._last_node_refresh = 0.0

        self.nodes: dict[str, dict] = {}
        self.addr_to_id: dict[str, str] = {}
        self._load_node_table()

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def node_table(self) -> dict[str, dict]:
        return self.nodes

    def _load_node_table(self) -> None:
        """Load id/ip/port/role/neighbors from config, scenario.json or live peers."""
        participant = self._engine.config.participant
        raw_nodes = (participant.get("trust_args") or {}).get("scenario", {}).get("nodes") or {}
        if not raw_nodes:
            raw_nodes = self._read_scenario_json()

        if isinstance(raw_nodes, list):
            raw_nodes = {str(node.get("id", index)): node for index, node in enumerate(raw_nodes)}
        for node_id, node in (raw_nodes or {}).items():
            ip = node.get("ip")
            port = node.get("port")
            if ip is None or port is None:
                continue
            addr = f"{ip}:{port}"
            node_id = str(node_id)
            self.nodes[node_id] = {
                "id": node_id,
                "addr": addr,
                "role": str(node.get("role", "")).lower(),
                "neighbors": {str(neighbor) for neighbor in node.get("neighbors", [])},
                "start": bool(node.get("start", False)),
            }
            self.addr_to_id[addr] = node_id

        self._resolve_neighbors()

        if not self.nodes:
            for addr in getattr(self._engine.cm, "connections", {}):
                node_id = addr
                self.nodes[node_id] = {
                    "id": node_id,
                    "addr": addr,
                    "role": "",
                    "neighbors": set(),
                }
                self.addr_to_id[addr] = node_id

    def _resolve_neighbors(self) -> None:
        for node in self.nodes.values():
            neighbors = set()
            for neighbor in node.get("neighbors", set()):
                if neighbor in self.nodes:
                    neighbors.add(neighbor)
                elif neighbor in self.addr_to_id:
                    neighbors.add(self.addr_to_id[neighbor])
                else:
                    neighbors.add(neighbor)
            node["neighbors"] = neighbors

    def _maybe_refresh_node_table(self) -> None:
        now = time.time()
        if now - self._last_node_refresh < 10.0:
            return
        self._last_node_refresh = now
        if not self.nodes or any(not node.get("role") for node in self.nodes.values()):
            self.nodes.clear()
            self.addr_to_id.clear()
            self._load_node_table()

    def _read_scenario_json(self) -> dict:
        tracking = self._engine.config.participant.get("tracking_args") or {}
        config_dir = os.environ.get("NEBULA_CONFIG_DIR") or tracking.get("config_dir", "")
        scenario_name = (self._engine.config.participant.get("scenario_args") or {}).get("name", "")
        candidates = []
        if config_dir:
            if scenario_name:
                candidates.append(os.path.join(config_dir, scenario_name, "scenario.json"))
            candidates.append(os.path.join(config_dir, "scenario.json"))
            if os.path.basename(config_dir.rstrip("/")) != "config":
                candidates.append(os.path.join(os.path.dirname(config_dir.rstrip("/")), "scenario.json"))
        for path in candidates:
            try:
                if os.path.isfile(path):
                    with open(path) as file:
                        scenario = json.load(file)
                    nodes = scenario.get("nodes") or {}
                    if isinstance(nodes, list):
                        return {str(node.get("id", index)): node for index, node in enumerate(nodes)}
                    if isinstance(nodes, dict):
                        return nodes
            except Exception as e:
                logging.debug(f"AFAS | cannot read scenario file {path}: {e}")
        return {}

    def node_id(self, addr: str) -> str:
        return self.addr_to_id.get(addr, addr)

    def node_role(self, addr: str) -> str:
        node = self.nodes.get(self.addr_to_id.get(addr, ""))
        return node.get("role", "") if node else ""

    def node_label(self, addr: str) -> str:
        node_id = self.addr_to_id.get(addr)
        return f"{node_id} / {addr}" if node_id else addr

    def cluster_members(self, addr: str) -> set[str]:
        """Return addr plus its direct scenario neighbors (local cluster)."""
        node_id = self.addr_to_id.get(addr)
        if not node_id or node_id not in self.nodes:
            return {addr}
        node = self.nodes[node_id]
        members = {addr}
        for neighbor_id in node.get("neighbors", set()):
            neighbor = self.nodes.get(neighbor_id)
            if neighbor:
                members.add(neighbor["addr"])
        return members

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._started_at = time.time()
        self._task = asyncio.create_task(self._run())
        logging.info("AFAS | network monitor started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._probe_round()
                await self._refresh_connection_status()
                self._maybe_refresh_node_table()
                await self._maybe_share_digest()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("AFAS | monitor loop error")
            await asyncio.sleep(self.probe_interval_s)

    async def _probe_round(self) -> None:
        targets = await self._probe_targets()
        now = time.time()
        for addr in targets:
            self._seq += 1
            seq = self._seq
            self._outstanding[seq] = (addr, now)
            payload = {"afas": AFAS_PROBE, "seq": seq, "sent_ts": now}
            await self._send_control("weak_link", payload, addr)

        await asyncio.sleep(self.probe_timeout_s)
        expired = time.time()
        for seq, (addr, sent_ts) in list(self._outstanding.items()):
            if expired - sent_ts >= self.probe_timeout_s:
                self._outstanding.pop(seq, None)
                self._append_sample(addr, False, None)

    async def _probe_targets(self) -> set[str]:
        try:
            addrs = await self._engine.cm.get_addrs_current_connections(only_direct=True, myself=False)
        except Exception:
            addrs = set(getattr(self._engine.cm, "connections", {}))
        return {addr for addr in addrs if addr != self.own_addr}

    async def _refresh_connection_status(self) -> None:
        connected = set(getattr(self._engine.cm, "connections", {}))
        for addr in self._known_connected - connected:
            for _ in range(self.failed_probes_threshold):
                self._append_sample(addr, False, None)
        self._known_connected = connected

    async def _maybe_share_digest(self) -> None:
        now = time.time()
        if now - self._last_digest < self.digest_interval_s:
            return
        self._last_digest = now
        table = {addr: metrics.as_dict(now) for addr, metrics in self.snapshot().items()}
        payload = {"afas": AFAS_METRICS, "ts": now, "table": table}
        for addr in await self._digest_targets():
            await self._send_control("overhead", payload, addr)

    async def _digest_targets(self) -> set[str]:
        try:
            direct = await self._engine.cm.get_addrs_current_connections(only_direct=True, myself=False)
        except Exception:
            direct = set()
        if self.federation != "CFL":
            return direct
        afas = getattr(self._engine, "_afas", None)
        current = getattr(afas, "current_aggregator", None)
        if current == self.own_addr:
            return direct
        return {current} if current in direct else direct

    async def _send_control(self, action: str, payload: dict, dest: str) -> None:
        if not dest or dest == self.own_addr:
            return
        try:
            message = self._engine.cm.create_message(
                "control",
                action,
                log=json.dumps(payload, separators=(",", ":")),
            )
            await self._engine.cm.send_message(dest, message)
        except Exception as e:
            logging.debug(f"AFAS | cannot send {payload.get('afas')} to {dest}: {e}")

    def _decode(self, message) -> dict | None:
        try:
            payload = json.loads(getattr(message, "log", "") or "{}")
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def handle_weak_link(self, source: str, message) -> None:
        payload = self._decode(message)
        if not payload:
            return
        kind = payload.get("afas")
        if kind == AFAS_PROBE:
            reply = {
                "afas": AFAS_PONG,
                "seq": payload.get("seq"),
                "sent_ts": payload.get("sent_ts"),
                "metrics": self.own_metrics().as_dict(),
            }
            await self._send_control("weak_link", reply, source)
        elif kind == AFAS_PONG:
            seq = payload.get("seq")
            sent = self._outstanding.pop(seq, None)
            if sent is None or sent[0] != source:
                return
            self._append_sample(source, True, (time.time() - sent[1]) * 1000.0)

    async def handle_overhead(self, source: str, message) -> None:
        payload = self._decode(message)
        if not payload or payload.get("afas") != AFAS_METRICS:
            return
        table = payload.get("table") or {}
        if not isinstance(table, dict):
            return
        now = time.time()
        for addr, entry in table.items():
            if not isinstance(entry, dict):
                continue
            previous = self._global.get(addr)
            ts = float(entry.get("ts", now))
            if previous is not None and previous.last_seen > ts:
                continue
            self._global[addr] = PeerMetrics(
                addr=addr,
                rtt_ms=_float_or_none(entry.get("rtt_ms")),
                jitter_ms=float(entry.get("jitter_ms") or 0.0),
                loss_pct=float(entry.get("loss_pct") or 0.0),
                consecutive_failures=int(entry.get("consecutive_failures") or 0),
                last_seen=ts,
                reachable=bool(entry.get("reachable")),
                source=f"gossip:{source}",
            )

    def note_disconnection(self, addr: str) -> None:
        for _ in range(self.failed_probes_threshold):
            self._append_sample(addr, False, None)

    def own_metrics(self) -> PeerMetrics:
        return PeerMetrics(
            addr=self.own_addr,
            rtt_ms=0.0,
            jitter_ms=0.0,
            loss_pct=0.0,
            consecutive_failures=0,
            last_seen=time.time(),
            reachable=True,
            source="self",
        )

    def _append_sample(self, addr: str, ok: bool, rtt_ms: float | None) -> None:
        if not addr or addr == self.own_addr:
            return
        self._history[addr].append({"ts": time.time(), "ok": ok, "rtt": rtt_ms})

    def _local_metrics(self, addr: str) -> PeerMetrics | None:
        samples = self._history.get(addr)
        if not samples:
            return None
        successes = [sample for sample in samples if sample["ok"] and sample["rtt"] is not None]
        failures = [sample for sample in samples if not sample["ok"]]
        consecutive = 0
        for sample in reversed(samples):
            if sample["ok"]:
                break
            consecutive += 1
        rtts = [sample["rtt"] for sample in successes]
        jitter = 0.0
        if len(rtts) > 1:
            jitter = statistics.mean(abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts)))
        loss = (len(failures) / len(samples)) * 100.0 if samples else 0.0
        connected = addr in getattr(self._engine.cm, "connections", {})
        return PeerMetrics(
            addr=addr,
            rtt_ms=statistics.mean(rtts) if rtts else None,
            jitter_ms=jitter,
            loss_pct=loss,
            consecutive_failures=consecutive,
            last_seen=samples[-1]["ts"],
            reachable=connected and consecutive < self.failed_probes_threshold,
            source="local",
        )

    def metrics_for(self, addr: str) -> PeerMetrics:
        if addr == self.own_addr:
            return self.own_metrics()
        local = self._local_metrics(addr)
        global_metrics = self._global.get(addr)
        if global_metrics is not None and time.time() - global_metrics.last_seen > self.metrics_ttl_s:
            global_metrics = None

        if local is not None and (global_metrics is None or local.last_seen >= global_metrics.last_seen):
            if local.rtt_ms is None and global_metrics is not None and global_metrics.rtt_ms is not None:
                return global_metrics
            return local
        if global_metrics is not None:
            return global_metrics
        return PeerMetrics(addr=addr, reachable=False, source="unknown")

    def snapshot(self) -> dict[str, PeerMetrics]:
        addrs = {self.own_addr} | set(self._history) | set(getattr(self._engine.cm, "connections", {})) | set(self._global)
        return {addr: self.metrics_for(addr) for addr in addrs}

    def score(self, addr: str) -> float:
        if addr == self.own_addr:
            global_self = self._global.get(addr)
            if global_self is not None and time.time() - global_self.last_seen <= self.metrics_ttl_s:
                return _score_metrics(global_self)
            peer_scores = [
                _score_metrics(metrics)
                for peer_addr, metrics in self.snapshot().items()
                if peer_addr != self.own_addr and metrics.reachable
            ]
            return statistics.median(peer_scores) if peer_scores else 0.0
        metrics = self.metrics_for(addr)
        if not metrics.reachable:
            return float("inf")
        return _score_metrics(metrics)

    def best_candidate(self, candidates: set[str]) -> str | None:
        viable = [candidate for candidate in candidates if self.score(candidate) != float("inf")]
        if not viable:
            return None
        return min(viable, key=lambda candidate: (self.score(candidate), candidate))

    def degraded_reasons(self, addr: str) -> list[str]:
        if not addr or addr == self.own_addr:
            return []
        metrics = self.metrics_for(addr)
        connected = addr in getattr(self._engine.cm, "connections", {})
        reasons: list[str] = []

        if self.federation == "CFL" and not connected:
            reasons.append("Sin conexión TCP con el agregador")

        if metrics.source == "unknown":
            return reasons

        if metrics.consecutive_failures >= self.failed_probes_threshold:
            reasons.append(
                f"Timeout consecutivo = {metrics.consecutive_failures} >= {self.failed_probes_threshold}"
            )
        if metrics.rtt_ms is not None and metrics.rtt_ms > self.rtt_threshold_ms:
            reasons.append(f"Latencia RTT = {_fmt(metrics.rtt_ms)}ms > {_fmt(self.rtt_threshold_ms)}ms")
        if metrics.loss_pct > self.loss_threshold_pct:
            reasons.append(f"Pérdida de paquetes = {_fmt(metrics.loss_pct)}% > {_fmt(self.loss_threshold_pct)}%")
        if metrics.jitter_ms > self.jitter_threshold_ms:
            reasons.append(f"Jitter = {_fmt(metrics.jitter_ms)}ms > {_fmt(self.jitter_threshold_ms)}ms")

        if (
            not metrics.reachable
            and metrics.consecutive_failures >= self.failed_probes_threshold
            and not any(reason.startswith("Sin conexión") for reason in reasons)
        ):
            reasons.append("Agregador inalcanzable por timeout de red")
        return reasons


def _score_metrics(metrics: PeerMetrics) -> float:
    if not metrics.reachable:
        return float("inf")
    rtt = metrics.rtt_ms if metrics.rtt_ms is not None else 0.0
    return rtt + (metrics.loss_pct * 10.0) + (metrics.jitter_ms * 0.5) + (metrics.consecutive_failures * 1000.0)


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fmt(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"
