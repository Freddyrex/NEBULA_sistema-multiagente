import asyncio
import logging
import subprocess
from functools import cached_property

from nebula.addons.networksimulation.networksimulator import NetworkSimulator
from nebula.config.defaults import (
    MULTICAST_IP,
    NET_COND_PRECISION,
    NETWORK_DISTANCE_THRESHOLDS,
    TC_DEFAULTS,
)
from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import GPSEvent
from nebula.core.network.communications import CommunicationsManager
from nebula.core.utils.locker import Locker


def _thresholds_to_map(thresholds) -> dict:
    """Convierte [{up_to_m, bandwidth, delay}] al mapa {límite: {...}} interno.

    §7: valida y ordena; falla alto ante tramos no crecientes o campos ausentes
    (nunca fallback silencioso a tabla global).
    """
    items = []
    for t in thresholds or []:
        try:
            up_to = float(t["up_to_m"]) if t["up_to_m"] != float("inf") else float("inf")
            items.append((up_to, {"bandwidth": str(t["bandwidth"]), "delay": str(t["delay"])}))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"network distance_thresholds inválido {t!r}: {e}") from e
    items.sort(key=lambda kv: kv[0])
    limits = [k for k, _ in items]
    if limits != sorted(limits) or len(set(limits)) != len(limits):
        raise ValueError(f"network distance_thresholds debe ser creciente y único: {thresholds!r}")
    return dict(items)


class NebulaNS(NetworkSimulator):
    NETWORK_CONDITIONS = _thresholds_to_map(NETWORK_DISTANCE_THRESHOLDS)
    IP_MULTICAST = MULTICAST_IP

    def __init__(self, changing_interval, interface, verbose=False):
        self._refresh_interval = changing_interval
        self._node_interface = interface
        self._verbose = verbose
        self._network_conditions = self.NETWORK_CONDITIONS.copy()
        self._network_conditions_lock = Locker("network_conditions_lock", async_lock=True)
        self._current_network_conditions = {}
        self._running = asyncio.Event()

    @cached_property
    def cm(self):
        return CommunicationsManager.get_instance()

    async def start(self):
        logging.info("🌐  Nebula Network Simulator starting...")
        self._running.set()
        grace_time = self.cm.config.participant["mobility_args"]["grace_time_mobility"]
        # Overrides por escenario (network_args): tabla de umbrales e IP multicast.
        # Si no existen, se usan los defaults de nebula.config.defaults.
        try:
            network_args = self.cm.config.participant.get("network_args", {}) or {}
            custom = network_args.get("distance_thresholds")
            if custom:
                # §7: validado (falla alto); sin fallback silencioso.
                self._network_conditions = _thresholds_to_map(custom)
            multicast = network_args.get("multicast_ip")
            if multicast:
                self.IP_MULTICAST = str(multicast)
            # §7: net_sim desconocido falla alto (no fallback silencioso).
            backend = str(network_args.get("net_sim", "") or "").strip().lower()
            if backend and backend not in ("", "tc", "tcset", "none", "nebulanetworksimulator"):
                raise ValueError(f"net_sim desconocido: {backend!r} (usa 'tc' o 'none')")
        except ValueError:
            raise
        except Exception:
            logging.exception("🌐  Error leyendo network_args, se usan thresholds por defecto")
        # if self._verbose: logging.info(f"Waiting {grace_time}s to start applying network conditions based on distances between devices")
        # await asyncio.sleep(grace_time)
        await EventManager.get_instance().subscribe_addonevent(
            GPSEvent, self._change_network_conditions_based_on_distances
        )

    async def stop(self):
        logging.info("🌐  Nebula Network Simulator stopping...")
        self._running.clear()

    async def is_running(self):
        return self._running.is_set()

    async def _change_network_conditions_based_on_distances(self, gpsevent: GPSEvent):
        distances = await gpsevent.get_event_data()
        if self._verbose:
            logging.info("Refresh | conditions based on distances...")
        try:
            for addr, (distance, _) in distances.items():
                if distance is None:
                    # If the distance is not found, we skip the node
                    continue
                conditions = await self._calculate_network_conditions(distance)
                # Only update the network conditions if they have changed
                if (
                    addr not in self._current_network_conditions
                    or self._current_network_conditions[addr] != conditions
                ):
                    addr_ip = addr.split(":")[0]
                    self._set_network_condition_for_addr(
                        self._node_interface, addr_ip, conditions["bandwidth"], conditions["delay"]
                    )
                    self._set_network_condition_for_multicast(
                        self._node_interface,
                        addr_ip,
                        self.IP_MULTICAST,
                        conditions["bandwidth"],
                        conditions["delay"],
                    )
                    async with self._network_conditions_lock:
                        self._current_network_conditions[addr] = conditions
                else:
                    if self._verbose:
                        logging.info("network conditions havent changed since last time")
        except KeyError:
            logging.exception(f"📍  Connection {addr} not found")
        except Exception:
            logging.exception("📍  Error changing connections based on distance")
        # §7: el throttle vive en el llamante periódico, no en el handler
        # (dormir aquí retrasa el bus de eventos GPS). Se mantiene por
        # compatibilidad pero documentado como debounce, no como tarea.
        await asyncio.sleep(self._refresh_interval)

    async def set_thresholds(self, thresholds: dict):
        async with self._network_conditions_lock:
            self._network_conditions = thresholds

    async def set_network_conditions(self, dest_addr: str, distance):
        conditions = await self._calculate_network_conditions(distance)
        addr_ip = dest_addr.split(":")[0]
        if (
            dest_addr not in self._current_network_conditions
            or self._current_network_conditions[dest_addr] != conditions
        ):
            self._set_network_condition_for_addr(
                interface=self._node_interface,
                network=addr_ip,
                bandwidth=conditions["bandwidth"],
                delay=conditions["delay"],
            )

            self._set_network_condition_for_multicast(
                interface=self._node_interface,
                src_network=addr_ip,
                dst_network=self.IP_MULTICAST,
                bandwidth=conditions["bandwidth"],
                delay=conditions["delay"],
            )
            async with self._network_conditions_lock:
                self._current_network_conditions[dest_addr] = conditions

    def _set_network_condition_for_addr(
        self,
        interface=TC_DEFAULTS["interface"],
        network="",
        bandwidth=TC_DEFAULTS["bandwidth"],
        delay=TC_DEFAULTS["delay"],
        delay_distro=TC_DEFAULTS["delay_distro"],
        delay_distribution=TC_DEFAULTS["delay_distribution"],
        loss=TC_DEFAULTS["loss"],
        duplicate=TC_DEFAULTS["duplicate"],
        corrupt=TC_DEFAULTS["corrupt"],
        reordering=TC_DEFAULTS["reordering"],
    ):
        if self._verbose:
            logging.info(
                f"🌐  Changing network conditions | Interface: {interface} | Network: {network} | Bandwidth: {bandwidth} | Delay: {delay} | Delay Distro: {delay_distro} | Delay Distribution: {delay_distribution} | Loss: {loss} | Duplicate: {duplicate} | Corrupt: {corrupt} | Reordering: {reordering}"
            )
        try:
            results = subprocess.run(
                [
                    "tcset",
                    str(interface),
                    "--network",
                    str(network) if network is not None else "",
                    "--rate",
                    str(bandwidth),
                    "--delay",
                    str(delay),
                    "--delay-distro",
                    str(delay_distro),
                    "--delay-distribution",
                    str(delay_distribution),
                    "--loss",
                    str(loss),
                    "--duplicate",
                    str(duplicate),
                    "--corrupt",
                    str(corrupt),
                    "--reordering",
                    str(reordering),
                    "--change",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            logging.exception(f"❗️  Network simulation error: {e}")
            return

    def _set_network_condition_for_multicast(
        self,
        interface=TC_DEFAULTS["interface"],
        src_network="",
        dst_network="",
        bandwidth=TC_DEFAULTS["bandwidth"],
        delay=TC_DEFAULTS["delay"],
        delay_distro=TC_DEFAULTS["delay_distro"],
        delay_distribution=TC_DEFAULTS["delay_distribution"],
        loss=TC_DEFAULTS["loss"],
        duplicate=TC_DEFAULTS["duplicate"],
        corrupt=TC_DEFAULTS["corrupt"],
        reordering=TC_DEFAULTS["reordering"],
    ):
        if self._verbose:
            logging.info(
                f"🌐  Changing multicast conditions | Interface: {interface} | Src Network: {src_network} | Bandwidth: {bandwidth} | Delay: {delay} | Delay Distro: {delay_distro} | Delay Distribution: {delay_distribution} | Loss: {loss} | Duplicate: {duplicate} | Corrupt: {corrupt} | Reordering: {reordering}"
            )

        try:
            results = subprocess.run(
                [
                    "tcset",
                    str(interface),
                    "--src-network",
                    str(src_network),
                    "--dst-network",
                    str(dst_network),
                    "--rate",
                    str(bandwidth),
                    "--delay",
                    str(delay),
                    "--delay-distro",
                    str(delay_distro),
                    "--delay-distribution",
                    str(delay_distribution),
                    "--loss",
                    str(loss),
                    "--duplicate",
                    str(duplicate),
                    "--corrupt",
                    str(corrupt),
                    "--reordering",
                    str(reordering),
                    "--direction",
                    "incoming",
                    "--change",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            logging.exception(f"❗️  Network simulation error: {e}")
            return

    async def _calculate_network_conditions(self, distance):
        def extract_number(value):
            import re

            match = re.match(r"([\d.]+)", value)
            if not match:
                raise ValueError(f"Formato inválido: {value}")
            return float(match.group(1))

        if self._verbose:
            logging.info(f"Calculating conditions for distance: {distance}m")
        conditions = None
        async with self._network_conditions_lock:
            th = self._network_conditions.copy()

        thresholds = sorted(th.keys())

        # Si la distancia es menor que el primer umbral, devolver la mejor condición
        if distance < thresholds[0]:
            conditions = {"bandwidth": th[thresholds[0]]["bandwidth"], "delay": th[thresholds[0]]["delay"]}

        # Encontrar el tramo en el que se encuentra la distancia
        for i in range(len(thresholds) - 1):
            lower_bound = thresholds[i]
            upper_bound = thresholds[i + 1]

            if upper_bound == float("inf"):
                break

            if lower_bound <= distance < upper_bound:
                # logging.info(f"Bounds | lower: {lower_bound} | upper: {upper_bound}")
                lower_cond = th[lower_bound]
                upper_cond = th[upper_bound]

                # Extraer valores numéricos y unidades
                lower_bandwidth_value = extract_number(lower_cond["bandwidth"])
                upper_bandwidth_value = extract_number(upper_cond["bandwidth"])
                lower_bandwidth_unit = lower_cond["bandwidth"].replace(str(lower_bandwidth_value), "")
                upper_bandwidth_unit = upper_cond["bandwidth"].replace(str(upper_bandwidth_value), "")

                lower_delay_value = extract_number(lower_cond["delay"])
                upper_delay_value = extract_number(upper_cond["delay"])
                delay_unit = lower_cond["delay"].replace(str(lower_delay_value), "")

                # Calcular el progreso en el tramo (0 a 1)
                progress = (distance - lower_bound) / (upper_bound - lower_bound)
                if self._verbose:
                    logging.info(f"Progress between the bounds: {progress}")

                # Interpolación lineal de valores
                bandwidth_value = lower_bandwidth_value - progress * (lower_bandwidth_value - upper_bandwidth_value)
                delay_value = lower_delay_value + progress * (upper_delay_value - lower_delay_value)

                # Reconstruir valores con unidades originales
                bandwidth = f"{round(bandwidth_value, NET_COND_PRECISION)}{lower_bandwidth_unit}"
                delay = f"{round(delay_value, NET_COND_PRECISION)}{delay_unit}"

                conditions = {"bandwidth": bandwidth, "delay": delay}

        # Si la distancia es infinita, devolver el último valor
        if not conditions:
            conditions = {"bandwidth": th[float("inf")]["bandwidth"], "delay": th[float("inf")]["delay"]}
        if self._verbose:
            logging.info(f"Network conditions: {conditions}")
        return conditions

    def clear_network_conditions(self, interface):
        if self._verbose:
            logging.info("🌐  Resetting network conditions")
        try:
            results = subprocess.run(
                ["tcdel", str(interface), "--all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            logging.exception(f"❗️  Network simulation error: {e}")
            return

    def _generate_network_conditions(self):
        # TODO: Implement selection of network conditions from frontend
        if self.config.participant["network_args"]["simulation"]:
            interface = self.config.participant["network_args"]["interface"]
            bandwidth = self.config.participant["network_args"]["bandwidth"]
            delay = self.config.participant["network_args"]["delay"]
            delay_distro = self.config.participant["network_args"]["delay-distro"]
            delay_distribution = self.config.participant["network_args"]["delay-distribution"]
            loss = self.config.participant["network_args"]["loss"]
            duplicate = self.config.participant["network_args"]["duplicate"]
            corrupt = self.config.participant["network_args"]["corrupt"]
            reordering = self.config.participant["network_args"]["reordering"]
            logging.info(
                f"🌐  Network simulation is enabled | Interface: {interface} | Bandwidth: {bandwidth} | Delay: {delay} | Delay Distro: {delay_distro} | Delay Distribution: {delay_distribution} | Loss: {loss} | Duplicate: {duplicate} | Corrupt: {corrupt} | Reordering: {reordering}"
            )
            try:
                results = subprocess.run(
                    [
                        "tcset",
                        str(interface),
                        "--rate",
                        str(bandwidth),
                        "--delay",
                        str(delay),
                        "--delay-distro",
                        str(delay_distro),
                        "--delay-distribution",
                        str(delay_distribution),
                        "--loss",
                        str(loss),
                        "--duplicate",
                        str(duplicate),
                        "--corrupt",
                        str(corrupt),
                        "--reordering",
                        str(reordering),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception as e:
                logging.exception(f"🌐  Network simulation error: {e}")
                return
        else:
            logging.info("🌐  Network simulation is disabled. Using default network conditions...")
