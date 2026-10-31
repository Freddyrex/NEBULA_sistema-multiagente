# ruff: noqa: S101
import asyncio
import json
import os
import tempfile
import time
from types import SimpleNamespace

from nebula.core.aggregatorfailover import AggregatorFailoverManager
from nebula.core.network.networkmonitor import NetworkMonitor, PeerMetrics
from nebula.core.noderole import Role


class _Lock:
    def __init__(self):
        self._lock = asyncio.Lock()

    async def locked_async(self):
        return self._lock.locked()

    async def acquire_async(self):
        await self._lock.acquire()

    async def release_async(self):
        if self._lock.locked():
            self._lock.release()


class _RoleBehavior:
    def __init__(self):
        self._role = Role.TRAINER
        self._next_role = None

    def get_role(self):
        return self._role

    def get_role_name(self, effective=False):
        return self._role.value

    async def set_next_role(self, role, source_to_notificate=None):
        self._next_role = role

    async def get_next_role(self):
        role, self._next_role = self._next_role, None
        return role


class _Connections:
    def __init__(self, addresses):
        self.connections = {addr: object() for addr in addresses}
        self.sent = []

    async def get_addrs_current_connections(self, only_direct=True, myself=False):
        return set(self.connections)

    async def connect(self, addr, direct=True):
        self.connections[addr] = object()
        return True

    def create_message(self, message_type, action="", **kwargs):
        return SimpleNamespace(action=action, **kwargs)

    async def send_message(self, dest, message, message_type=""):
        self.sent.append((dest, message))


class _Updates:
    async def get_round_updates(self):
        return {"trainer-a:4001": ({"weight": 1}, 2.0)}


class _Engine:
    def __init__(self, addr, role, federation, nodes):
        self.addr = addr
        self.round = 5
        self.total_rounds = 20
        self.role = role
        self._global_model_source = None
        self._round_in_process_lock = _Lock()
        self.rb = _RoleBehavior()
        self.rb._role = Role(role)
        self.cm = _Connections({n["addr"] for n in nodes if n["addr"] != addr})
        self.aggregator = SimpleNamespace(us=_Updates())
        self.trainer = SimpleNamespace(
            serialize_model=lambda model: json.dumps(model).encode(),
            deserialize_model=lambda data: json.loads(data),
        )
        self.config = SimpleNamespace(
            participant={
                "scenario_args": {"name": "afas-test", "federation": federation},
                "network_monitor_args": {
                    "startup_grace_s": 0,
                    "election_cooldown_s": 0,
                    "degrade_confirm_s": 0,
                },
                "tracking_args": {"log_dir": tempfile.mkdtemp(), "config_dir": ""},
                "trust_args": {
                    "scenario": {
                        "nodes": {
                            str(index): node for index, node in enumerate(nodes)
                        }
                    }
                },
            }
        )

    async def update_self_role(self):
        if self.rb._next_role is not None:
            self.rb._role = self.rb._next_role
            self.rb._next_role = None
            self.role = self.rb._role.value
            self.aggregator.us._role = self.role


def _cfl_nodes():
    return [
        {"ip": "10.0.0.1", "port": "4001", "role": "server", "start": True, "neighbors": []},
        {"ip": "10.0.0.2", "port": "4001", "role": "trainer", "start": False, "neighbors": []},
        {"ip": "10.0.0.3", "port": "4001", "role": "trainer", "start": False, "neighbors": []},
    ]


def test_cfl_failover_switches_to_lowest_latency_peer():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.3:4001", "trainer", "CFL", nodes)
        monitor = NetworkMonitor(engine)
        afas = AggregatorFailoverManager(engine, monitor)
        monitor._started_at = time.time() - 60
        afas.current_aggregator = "10.0.0.1:4001"

        for _ in range(3):
            monitor._append_sample("10.0.0.1:4001", False, None)
        monitor._append_sample("10.0.0.3:4001", True, 10.0)
        monitor._global["10.0.0.2:4001"] = PeerMetrics(
            addr="10.0.0.2:4001", rtt_ms=100.0, last_seen=time.time(), reachable=True
        )
        monitor._global["10.0.0.3:4001"] = PeerMetrics(
            addr="10.0.0.3:4001", rtt_ms=10.0, last_seen=time.time(), reachable=True
        )

        await afas.evaluate()
        await afas.evaluate()

        assert afas.current_aggregator == "10.0.0.3:4001"
        assert engine.rb.get_role() == Role.AGGREGATOR
        log_file = os.path.join(
            engine.config.participant["tracking_args"]["log_dir"],
            "afas-test",
            "smac_decisions.jsonl",
        )
        with open(log_file) as file:
            record = json.loads(file.readline())
        assert record["owner"] == "AFAS"
        assert record["action"] == "select_aggregator"
        assert "Cambio Dinámico de Agregador" in record["explanation"]

    asyncio.run(scenario())


def test_buffer_transfer_is_kept_for_replay():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        old = _Engine("10.0.0.1:4001", "server", "CFL", nodes)
        new = _Engine("10.0.0.2:4001", "trainer", "CFL", nodes)
        old_afas = AggregatorFailoverManager(old, NetworkMonitor(old))
        new_afas = AggregatorFailoverManager(new, NetworkMonitor(new))

        await old_afas._send_buffer("10.0.0.2:4001", 123)
        dest, message = old.cm.sent[0]
        assert dest == "10.0.0.2:4001"
        await new_afas.handle_buffer_transfer("10.0.0.1:4001", message)

        assert new_afas._transferred["term"] == 123
        assert set(new_afas._transferred["updates"]) == {"trainer-a:4001"}

    asyncio.run(scenario())


def test_sdfl_prefers_intra_cluster_candidate_over_faster_inter_cluster():
    async def scenario():
        nodes = [
            {"ip": "10.0.0.1", "port": "4001", "role": "aggregator", "start": True, "neighbors": ["1"]},
            {"ip": "10.0.0.2", "port": "4001", "role": "trainer", "start": False, "neighbors": ["0", "2"]},
            {"ip": "10.0.0.3", "port": "4001", "role": "trainer", "start": False, "neighbors": ["1"]},
            {"ip": "10.0.0.4", "port": "4001", "role": "trainer", "start": False, "neighbors": []},
        ]
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.2:4001", "trainer", "SDFL", nodes)
        monitor = NetworkMonitor(engine)
        afas = AggregatorFailoverManager(engine, monitor)
        monitor._started_at = time.time() - 60
        afas.current_aggregator = "10.0.0.1:4001"

        for _ in range(3):
            monitor._append_sample("10.0.0.1:4001", False, None)
        now = time.time()
        for addr, rtt in (("10.0.0.2:4001", 200.0), ("10.0.0.3:4001", 100.0), ("10.0.0.4:4001", 1.0)):
            monitor._global[addr] = PeerMetrics(addr=addr, rtt_ms=rtt, last_seen=now, reachable=True)

        await afas.evaluate()
        await afas.evaluate()

        assert afas.current_aggregator == "10.0.0.3:4001"

    asyncio.run(scenario())


def test_monitor_loop_refreshes_node_table_without_crashing(caplog):
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.2:4001", "trainer", "CFL", nodes)
        monitor = NetworkMonitor(engine)
        monitor.probe_interval_s = 0.01
        monitor.probe_timeout_s = 0.01
        monitor.digest_interval_s = 60.0
        try:
            await monitor.start()
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()
        assert monitor._last_node_refresh >= 0.0

    asyncio.run(scenario())
    assert "AFAS | monitor loop error" not in caplog.text


def test_probe_pong_round_trip_and_gossip_populate_metrics():
    from nebula.core.network.networkmonitor import AFAS_METRICS, AFAS_PROBE

    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        first = _Engine("10.0.0.1:4001", "trainer", "CFL", nodes)
        second = _Engine("10.0.0.2:4001", "trainer", "CFL", nodes)
        monitor_a = NetworkMonitor(first)
        monitor_b = NetworkMonitor(second)

        monitor_a._outstanding[7] = (second.addr, time.time())
        await monitor_a._send_control(
            "weak_link", {"afas": AFAS_PROBE, "seq": 7, "sent_ts": time.time()}, second.addr
        )
        _, probe = first.cm.sent[-1]
        await monitor_b.handle_weak_link(first.addr, probe)

        _, pong = second.cm.sent[-1]
        await monitor_a.handle_weak_link(second.addr, pong)
        metrics = monitor_a.metrics_for(second.addr)
        assert metrics.rtt_ms is not None and metrics.reachable
        assert metrics.consecutive_failures == 0

        table = {first.addr: monitor_a.own_metrics().as_dict()}
        await monitor_a._send_control(
            "overhead", {"afas": AFAS_METRICS, "ts": time.time(), "table": table}, second.addr
        )
        _, digest = first.cm.sent[-1]
        await monitor_b.handle_overhead(first.addr, digest)
        assert monitor_b._global[first.addr].reachable

    asyncio.run(scenario())


def test_switch_sends_buffer_announces_and_steps_down_old_aggregator():
    from nebula.core.eventmanager import EventManager
    from nebula.core.nebulaevents import UpdateReceivedEvent

    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        old = _Engine("10.0.0.1:4001", "server", "CFL", nodes)
        new = _Engine("10.0.0.2:4001", "trainer", "CFL", nodes)
        old_afas = AggregatorFailoverManager(old, NetworkMonitor(old))
        new_afas = AggregatorFailoverManager(new, NetworkMonitor(new))

        await old_afas._execute_switch(old.addr, new.addr, ["Latencia RTT = 500ms > 200ms"])

        assert old_afas.current_aggregator == new.addr
        assert old.rb.get_role() == Role.TRAINER
        assert not await old._round_in_process_lock.locked_async()
        assert old._afas_leadership_hold_until == old.round + old_afas.switch_hold_rounds

        buffer_message = next(msg for _, msg in old.cm.sent if getattr(msg, "round", None) == -2)
        await new_afas.handle_buffer_transfer(old.addr, buffer_message)
        assert new_afas._transferred is not None
        assert set(new_afas._transferred["updates"]) == {"trainer-a:4001"}

        received = []

        async def capture(event):
            received.append(await event.get_event_data())

        manager = EventManager.get_instance()
        await manager.subscribe_node_event(UpdateReceivedEvent, capture)
        try:
            new.round = old.round
            await new_afas.replay_transferred_updates()
        finally:
            await manager.unsubscribe_event(UpdateReceivedEvent, capture)

        assert received and received[0][2] == "trainer-a:4001"
        log_file = os.path.join(
            old.config.participant["tracking_args"]["log_dir"],
            "afas-test",
            "smac_decisions.jsonl",
        )
        with open(log_file) as file:
            records = [json.loads(line) for line in file if line.strip()]
        assert records[0]["action"] == "select_aggregator"
        assert records[0]["extra"]["metrics_only"] is True

    asyncio.run(scenario())


def test_announcement_steps_down_stale_aggregator_and_ignores_old_terms():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        stale = _Engine("10.0.0.2:4001", "aggregator", "CFL", nodes)
        stale_afas = AggregatorFailoverManager(stale, NetworkMonitor(stale))
        stale_afas.term = 5
        stale_afas.current_aggregator = stale.addr

        current = {"afas": "announce", "term": 6, "old": stale.addr,
                   "new": "10.0.0.3:4001", "cause": "Timeout consecutivo"}
        await stale_afas.handle_control("10.0.0.3:4001", SimpleNamespace(log=json.dumps(current)))
        assert stale_afas.current_aggregator == "10.0.0.3:4001"
        assert stale_afas.term == 6
        assert stale.rb.get_role() == Role.TRAINER

        old = {"afas": "announce", "term": 4, "old": "10.0.0.3:4001",
               "new": stale.addr, "cause": "mensaje atrasado"}
        await stale_afas.handle_control("10.0.0.3:4001", SimpleNamespace(log=json.dumps(old)))
        assert stale_afas.current_aggregator == "10.0.0.3:4001"
        assert stale_afas.term == 6

    asyncio.run(scenario())


def test_alert_has_exact_required_text_and_is_emitted_once(capsys):
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.3:4001", "trainer", "CFL", nodes)
        afas = AggregatorFailoverManager(engine, NetworkMonitor(engine))
        await afas._alert("10.0.0.1:4001", "10.0.0.2:4001", "Latencia RTT = 500ms", 9)
        await afas._alert("10.0.0.1:4001", "10.0.0.2:4001", "Latencia RTT = 500ms", 9)

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert output.count("[AFAS ALERT] 🔄 Cambio Dinámico de Agregador Ejecutado") == 1
    assert "• Arquitectura: Centralizada" in output
    assert "• Agregador Anterior: 0 / 10.0.0.1:4001" in output
    assert "• Nuevo Agregador: 1 / 10.0.0.2:4001" in output
    assert "• Causa (Métrica de Red): Latencia RTT = 500ms" in output
    assert "• Estado del Entrenamiento: Continuo y activo (sin pausa)." in output


def test_fresh_gossip_wins_over_stale_local_sample():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.3:4001", "trainer", "CFL", nodes)
        monitor = NetworkMonitor(engine)
        stale = "10.0.0.1:4001"
        monitor._append_sample(stale, True, 10.0)
        monitor._history[stale][-1]["ts"] = time.time() - 300
        monitor._global[stale] = PeerMetrics(
            addr=stale, rtt_ms=500.0, loss_pct=20.0, last_seen=time.time(), reachable=True
        )

        metrics = monitor.metrics_for(stale)
        assert metrics.rtt_ms == 500.0
        assert any("Latencia" in reason for reason in monitor.degraded_reasons(stale))

    asyncio.run(scenario())


def test_abrupt_connection_loss_counts_as_consecutive_timeouts():
    async def scenario():
        nodes = [
            {"ip": "10.0.0.1", "port": "4001", "role": "aggregator", "start": True, "neighbors": ["1"]},
            {"ip": "10.0.0.2", "port": "4001", "role": "trainer", "start": False, "neighbors": ["0"]},
        ]
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.2:4001", "trainer", "SDFL", nodes)
        monitor = NetworkMonitor(engine)
        aggregator = "10.0.0.1:4001"
        for _ in range(20):
            monitor._append_sample(aggregator, True, 10.0)
        monitor._known_connected = {aggregator}
        engine.cm.connections.pop(aggregator)

        await monitor._refresh_connection_status()

        metrics = monitor.metrics_for(aggregator)
        assert metrics.consecutive_failures >= monitor.failed_probes_threshold
        assert monitor.degraded_reasons(aggregator)

    asyncio.run(scenario())


def test_afas_control_and_buffer_messages_serialize_through_protobuf():
    import zlib

    from nebula.core.network import messages as network_messages
    from nebula.core.network.actions import ControlAction

    manager = network_messages.MessagesManager("10.0.0.1:4001", {})
    control_data = manager.create_message(
        "control", "weak_link", log='{"afas":"afas_probe","seq":1}'
    )
    wrapper = network_messages.nebula_pb2.Wrapper()
    wrapper.ParseFromString(control_data)
    assert wrapper.control_message.action == ControlAction.WEAK_LINK.value
    assert json.loads(wrapper.control_message.log)["afas"] == "afas_probe"

    payload = zlib.compress(b'{"afas":"buffer","term":7}')
    model_data = manager.create_message("model", round=-2, parameters=payload, weight=1)
    wrapper.ParseFromString(model_data)
    assert wrapper.model_message.round == -2
    assert wrapper.model_message.parameters == payload


def test_engine_model_callback_routes_round_minus_two_to_afas():
    from nebula.core.engine import Engine

    class FakeAfas:
        def __init__(self):
            self.calls = []

        async def handle_buffer_transfer(self, source, message):
            self.calls.append((source, message.round))

    class FakeEngine:
        _afas = FakeAfas()

    message = SimpleNamespace(round=-2, parameters=b"buffer", weight=1.0)
    asyncio.run(Engine.model_update_callback(FakeEngine(), "10.0.0.1:4001", message))
    assert FakeEngine._afas.calls == [("10.0.0.1:4001", -2)]


def test_buffer_send_builds_valid_protobuf_message():
    from nebula.core.network import messages as network_messages

    class SerializingConnections(_Connections):
        def __init__(self, addr, addresses):
            super().__init__(addresses)
            self._manager = network_messages.MessagesManager(addr, {})

        def create_message(self, message_type, action="", **kwargs):
            return self._manager.create_message(message_type, action, **kwargs)

    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        old = _Engine("10.0.0.1:4001", "server", "CFL", nodes)
        old.cm = SerializingConnections(old.addr, ["10.0.0.2:4001"])
        afas = AggregatorFailoverManager(old, NetworkMonitor(old))
        await afas._send_buffer("10.0.0.2:4001", 7)
        _, data = old.cm.sent[-1]
        wrapper = network_messages.nebula_pb2.Wrapper()
        wrapper.ParseFromString(data)
        assert wrapper.model_message.round == -2
        assert wrapper.model_message.weight == 1

    asyncio.run(scenario())


def test_recovered_old_aggregator_replays_last_announcement():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.1:4001", "server", "CFL", nodes)
        afas = AggregatorFailoverManager(engine, NetworkMonitor(engine))
        afas.current_aggregator = engine.addr
        afas._last_announce = {
            "afas": "announce", "term": 11, "old": engine.addr,
            "new": "10.0.0.2:4001", "cause": "RTT alto", "architecture": "Centralizada",
        }
        afas._announced_peers.clear()

        await afas._ensure_connectivity(force=True)

        announcements = [
            (dest, message) for dest, message in engine.cm.sent
            if getattr(message, "action", "") == "recovery"
        ]
        assert announcements
        dest, message = announcements[0]
        assert json.loads(message.log)["term"] == 11
        assert dest in afas._announced_peers

    asyncio.run(scenario())


def test_buffer_transfer_rejects_stale_term_and_oversized_payload():
    async def scenario():
        nodes = _cfl_nodes()
        nodes = [dict(node, addr=f"{node['ip']}:{node['port']}") for node in nodes]
        engine = _Engine("10.0.0.2:4001", "trainer", "CFL", nodes)
        afas = AggregatorFailoverManager(engine, NetworkMonitor(engine))
        afas.term = 10

        old = _Engine("10.0.0.1:4001", "server", "CFL", nodes)
        old_afas = AggregatorFailoverManager(old, NetworkMonitor(old))
        await old_afas._send_buffer(engine.addr, 9)
        _, stale = old.cm.sent[-1]
        await afas.handle_buffer_transfer(old.addr, stale)
        assert afas._transferred is None

        afas.max_buffer_transfer_bytes = 4
        await afas.handle_buffer_transfer(old.addr, SimpleNamespace(parameters=b"0123456789"))
        assert afas._transferred is None

    asyncio.run(scenario())
