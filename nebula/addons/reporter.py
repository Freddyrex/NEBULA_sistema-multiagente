import asyncio
import importlib
import json
import logging
import os
import sys
from typing import TYPE_CHECKING

import aiohttp
import psutil

from nebula.config.defaults import (
    BYTES_PER_MB,
    CPU_SENSOR_NAMES,
    MW_TO_W,
    NET_MB_PRECISION,
    REPORTER_CONFIG_RELOAD_CYCLES,
    REPORTER_CPU_SAMPLE_INTERVAL_S,
    REPORTER_RETRY_DELAY_S,
)
from nebula.core.utils.thermal_energy import (
    C_BASE_W,
    ENERGY_EST_TAG,
    ENERGY_TOTAL_TAG,
    GPU_TEMP_EST_TAG,
    P_CPU_MAX_W,
    TEMP_SEED,
    CPU_TEMP_EST_TAG,
    cpu_power_from_util,
    estimate_energy_physical,
    estimate_energy_total,
    gpu_temp_est_tag,
    predict_cpu_temp_arx,
    predict_gpu_temp_arx,
)

if TYPE_CHECKING:
    pass


class Reporter:
    def __init__(self, config, trainer):
        """
        Initializes the reporter module for sending periodic updates to a dashboard controller.

        This initializer sets up the configuration parameters required to report metrics and statistics
        about the network, participant, and trainer. It connects to a specified URL endpoint where
        these metrics will be logged, and it initializes values used for tracking network traffic.

        Args:
            config (dict): The configuration dictionary containing all setup parameters.
            trainer (Trainer): The trainer object responsible for managing training sessions.
            cm (CommunicationsManager): The communications manager handling network connections
                                        and interactions.

        Attributes:
            frequency (int): The frequency at which the reporter sends updates.
            grace_time (int): Grace period before starting the reporting.
            data_queue (Queue): An asyncio queue for managing data to be reported.
            url (str): The endpoint URL for reporting updates.
            counter (int): Counter for tracking the number of reports sent.
            first_net_metrics (bool): Flag indicating if this is the first collection of network metrics.
            prev_bytes_sent (int), prev_bytes_recv (int), prev_packets_sent (int), prev_packets_recv (int):
                Metrics for tracking network data sent and received.
            acc_bytes_sent (int), acc_bytes_recv (int), acc_packets_sent (int), acc_packets_recv (int):
                Accumulators for network traffic.

        Raises:
            None

        Notes:
            - Logs the start of the reporter module.
            - Initializes both current and accumulated metrics for traffic monitoring.
        """
        logging.info("Starting reporter module")
        self._cm = None
        self.config = config
        self.trainer = trainer
        self.frequency = self.config.participant["reporter_args"]["report_frequency"]
        self.grace_time = self.config.participant["reporter_args"]["grace_time_reporter"]
        self.data_queue = asyncio.Queue()
        self.url = f"http://{self.config.participant['scenario_args']['controller']}/nodes/{self.config.participant['scenario_args']['name']}/update"
        self.counter = 0

        self.first_net_metrics = True
        self.prev_bytes_sent = 0
        self.prev_bytes_recv = 0
        self.prev_packets_sent = 0
        self.prev_packets_recv = 0

        self.acc_bytes_sent = 0
        self.acc_bytes_recv = 0
        self.acc_packets_sent = 0
        self.acc_packets_recv = 0
        # Calibración térmica/energética por dispositivo (thermal_args opcional
        # en el config del participante; si falta, van los defaults del modelo).
        thermal_args = (self.config.participant.get("thermal_args", {}) or {})
        self._arx_cpu = thermal_args.get("arx_cpu")
        self._arx_gpu = thermal_args.get("arx_gpu")
        self._energy_w = thermal_args.get("energy_weights")
        self._cpu_tdp = float(thermal_args.get("cpu_tdp_w", P_CPU_MAX_W))
        self._base_w = float(thermal_args.get("base_power_w", C_BASE_W))
        self._temp_seed = float(thermal_args.get("temp_seed_c", TEMP_SEED))
        # Estado ARX térmico: se encadena T[k] estimado -> T[k+1].
        self._t_cpu_prev: float | None = None
        self._t_gpu_prev: dict[int | str, float] = {}
        # Sprint 6: última ronda con checkpoint (evita duplicados).
        self._last_checkpoint_round: int | None = None
        self._running = asyncio.Event()
        self._reporter_task = None  # Track the background task

    @property
    def cm(self):
        if not self._cm:
            from nebula.core.network.communications import CommunicationsManager

            self._cm = CommunicationsManager.get_instance()
            return self._cm
        else:
            return self._cm

    async def enqueue_data(self, name, value):
        """
        Asynchronously enqueues data for reporting.

        This function adds a named data value pair to the data queue, which will later be processed
        and sent to the designated reporting endpoint. The queue enables handling of reporting tasks
        independently of other processes.

        Args:
            name (str): The name or identifier for the data item.
            value (Any): The value of the data item to be reported.

        Returns:
            None

        Notes:
            - This function is asynchronous to allow non-blocking data enqueueing.
            - Uses asyncio's queue to manage data, ensuring concurrency.
        """
        await self.data_queue.put((name, value))

    async def start(self):
        """
        Starts the reporter module after a grace period.

        This asynchronous function initiates the reporting process following a designated grace period.
        It creates a background task to run the reporting loop, allowing data to be reported at defined intervals.

        Returns:
            asyncio.Task: The task for the reporter loop, which handles the data reporting asynchronously.

        Notes:
            - The grace period allows for a delay before the first reporting cycle.
            - The reporter loop runs in the background, ensuring continuous data updates.
        """
        self._running.set()
        await asyncio.sleep(self.grace_time)
        self._reporter_task = asyncio.create_task(self.run_reporter(), name="Reporter_run_reporter")
        return self._reporter_task

    async def run_reporter(self):
        """
        Runs the continuous reporting loop.

        This asynchronous function performs periodic reporting tasks such as reporting resource usage,
        data queue contents, and, optionally, status updates to the controller. The loop runs indefinitely,
        updating the counter with each cycle to track the frequency of specific tasks.

        Key Actions:
            - Regularly reports the resource status.
            - Reloads the configuration file every 50 cycles to reflect any updates.

        Notes:
            - The reporting frequency is determined by the 'report_frequency' setting in the config file.
        """
        while self._running.is_set():
            if self.config.participant["reporter_args"]["report_status_data_queue"]:
                if self.config.participant["scenario_args"]["controller"] != "nebula-test":
                    await self.__report_status_to_controller()
                await self.__report_data_queue()
            await self.__report_resources()
            await self.__maybe_checkpoint()
            self.counter += 1
            if self.counter % REPORTER_CONFIG_RELOAD_CYCLES == 0:
                logging.info("Reloading config file...")
                self.cm.engine.config.reload_config_file()
            await asyncio.sleep(self.frequency)

    async def report_scenario_finished(self):
        """
        Reports the scenario completion status to the controller.

        This asynchronous function notifies the scenario controller that the participant has finished
        its tasks. It sends a POST request to the designated controller URL, including the participant's
        ID in the JSON payload.

        URL Construction:
            - The URL is dynamically built using the controller address and scenario name
              from the configuration settings.

        Parameters:
            - idx (int): The unique identifier for this participant, sent in the request data.

        Returns:
            - bool: True if the report was successful (status 200), False otherwise.

        Error Handling:
            - Logs an error if the response status is not 200, indicating that the controller
              might be temporarily overloaded.
            - Logs exceptions if the connection attempt to the controller fails.
        """
        url = f"http://{self.config.participant['scenario_args']['controller']}/nodes/{self.config.participant['scenario_args']['name']}/done"
        data = json.dumps({"idx": self.config.participant["device_args"]["idx"]})
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"NEBULA Participant {self.config.participant['device_args']['idx']}",
        }
        try:
            await self.__report_status_to_controller()
        except Exception as e:
            logging.exception(f"Error reporting status before scenario finished: {e}")
        try:
            async with aiohttp.ClientSession() as session, session.post(url, data=data, headers=headers) as response:
                if response.status != 200:
                    logging.error(
                        f"Error received from controller: {response.status} (probably there is overhead in the controller, trying again in the next round)"
                    )
                    text = await response.text()
                    logging.debug(text)
                else:
                    logging.info(
                        f"Participant {self.config.participant['device_args']['idx']} reported scenario finished"
                    )
                    return True
        except aiohttp.ClientError:
            logging.exception(f"Error connecting to the controller at {url}")
        return False

    async def stop(self):
        logging.info("🔍  Stopping reporter module...")
        self._running.clear()

        # Cancel the background task
        if self._reporter_task and not self._reporter_task.done():
            logging.info("🛑  Cancelling Reporter background task...")
            self._reporter_task.cancel()
            try:
                await self._reporter_task
            except asyncio.CancelledError:
                pass
            self._reporter_task = None
            logging.info("🛑  Reporter background task cancelled")

    async def __report_data_queue(self):
        """
        Processes and reports queued data entries.

        This asynchronous function iterates over the data queue, retrieving each name-value pair
        and sending it to the trainer's logging mechanism. Once logged, each item is marked as done.

        Functionality:
            - Retrieves and logs all entries in the data queue until it is empty.
            - Assumes that `log_data` can handle asynchronous execution for optimal performance.

        Parameters:
            - name (str): The identifier for the data entry (e.g., metric name).
            - value (Any): The value of the data entry to be logged.

        Returns:
            - None

        Notes:
            - Each processed item is marked as done in the queue.
        """

        while not self.data_queue.empty():
            name, value = await self.data_queue.get()
            await self.trainer.logger.log_data({name: value})  # Assuming log_data can be made async
            self.data_queue.task_done()

    async def __report_status_to_controller(self):
        """
        Sends the participant's status to the controller.

        This asynchronous function transmits the current participant configuration to the controller's
        URL endpoint. It handles both client and general exceptions to ensure robust communication
        with the controller, retrying in case of errors.

        Functionality:
            - Initiates a session to post participant data to the controller.
            - Logs the response status, indicating issues when status is non-200.
            - Retries after a short delay in case of connection errors or unhandled exceptions.

        Parameters:
            - None (uses internal `self.config.participant` data to build the payload).

        Returns:
            - None

        Notes:
            - Uses the participant index to specify the User-Agent in headers.
            - Delays for 5 seconds upon general exceptions to avoid rapid retry loops.
        """
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self.url,
                    data=json.dumps(self.config.participant),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": f"NEBULA Participant {self.config.participant['device_args']['idx']}",
                    },
                ) as response,
            ):
                if response.status != 200:
                    logging.error(
                        f"Error received from controller: {response.status} (probably there is overhead in the controller, trying again in the next round)"
                    )
                    text = await response.text()
                    logging.debug(text)
        except aiohttp.ClientError:
            logging.exception(f"Error connecting to the controller at {self.url}")
        except Exception:
            logging.exception("Error sending status to controller, will try again in a few seconds")
            await asyncio.sleep(REPORTER_RETRY_DELAY_S)

    async def __report_resources(self):
        """
        Reports system resource usage metrics.

        This asynchronous function gathers and logs CPU usage data for the participant's device,
        and attempts to retrieve the CPU temperature (Linux systems only). Additionally, it measures
        CPU usage specifically for the current process.

        Functionality:
            - Gathers total CPU usage (percentage) and attempts to retrieve CPU temperature
              (tries CPU_SENSOR_NAMES on Linux; None when unavailable).
              Sprint 1 (ROADMAP_AGENTE_ADAPTATIVO, regla 5): when no sensor exists
              the temperature tag is OMITTED (never logged as 0); the frontend
              marks it `unavailable` and the ARX estimate stays labeled `estimated`.
            - Uses `psutil` for non-blocking access to system data on Linux.
            - Records CPU usage of the current process for finer monitoring.

        Parameters:
            - None

        Notes:
            - On non-Linux platforms, CPU temperature will be None (tag omitted).
            - Uses `asyncio.to_thread` to call CPU and sensor readings without blocking the event loop.
        """
        cpu_percent = psutil.cpu_percent()
        cpu_temp = None
        try:
            if sys.platform == "linux":
                sensors = await asyncio.to_thread(psutil.sensors_temperatures)
                for name in CPU_SENSOR_NAMES:
                    entries = (sensors or {}).get(name)
                    if entries:
                        cpu_temp = entries[0].current
                        break
        except Exception:  # noqa: S110
            pass

        pid = os.getpid()
        cpu_percent_process = await asyncio.to_thread(
            psutil.Process(pid).cpu_percent, interval=REPORTER_CPU_SAMPLE_INTERVAL_S
        )

        process = psutil.Process(pid)
        memory_process = await asyncio.to_thread(lambda: process.memory_info().rss / BYTES_PER_MB)
        memory_percent_process = process.memory_percent()
        memory_info = await asyncio.to_thread(psutil.virtual_memory)
        memory_percent = memory_info.percent
        memory_used = memory_info.used / BYTES_PER_MB

        disk_percent = psutil.disk_usage("/").percent

        net_io_counters = await asyncio.to_thread(psutil.net_io_counters)
        bytes_sent = net_io_counters.bytes_sent
        bytes_recv = net_io_counters.bytes_recv
        packets_sent = net_io_counters.packets_sent
        packets_recv = net_io_counters.packets_recv

        if self.first_net_metrics:
            bytes_sent_diff = 0
            bytes_recv_diff = 0
            packets_sent_diff = 0
            packets_recv_diff = 0
            self.first_net_metrics = False
        else:
            bytes_sent_diff = bytes_sent - self.prev_bytes_sent
            bytes_recv_diff = bytes_recv - self.prev_bytes_recv
            packets_sent_diff = packets_sent - self.prev_packets_sent
            packets_recv_diff = packets_recv - self.prev_packets_recv

        self.prev_bytes_sent = bytes_sent
        self.prev_bytes_recv = bytes_recv
        self.prev_packets_sent = packets_sent
        self.prev_packets_recv = packets_recv

        self.acc_bytes_sent += bytes_sent_diff
        self.acc_bytes_recv += bytes_recv_diff
        self.acc_packets_sent += packets_sent_diff
        self.acc_packets_recv += packets_recv_diff

        current_connections = await self.cm.get_addrs_current_connections(only_direct=True)

        resources = {
            "W-CPU/CPU global (%)": cpu_percent,
            "W-CPU/CPU process (%)": cpu_percent_process,
            "Z-RAM/RAM global (%)": memory_percent,
            "Z-RAM/RAM global (MB)": memory_used,
            "Z-RAM/RAM process (%)": memory_percent_process,
            "Z-RAM/RAM process (MB)": memory_process,
            "Y-Disk/Disk (%)": disk_percent,
            "X-Network/Network (MB sent)": round(self.acc_bytes_sent / BYTES_PER_MB, NET_MB_PRECISION),
            "X-Network/Network (MB received)": round(self.acc_bytes_recv / BYTES_PER_MB, NET_MB_PRECISION),
            "X-Network/Network (packets sent)": self.acc_packets_sent,
            "X-Network/Network (packets received)": self.acc_packets_recv,
            "X-Network/Connections": len(current_connections),
        }
        # Sprint 1, regla 5: sin sensor no se loguea el tag (nada de 0 inventado).
        # Corridas viejas con 0 se filtran en el provider (ver _process_mapped_tag).
        if cpu_temp is not None:
            resources["W-CPU/CPU temperature (°)"] = cpu_temp
        self.trainer.logger.log_data(resources)

        # --- Métricas derivadas: T estimada (ARX) + consumo (E_total/E_est) ---
        # Se loguean con tags propios para que aparezcan en TensorBoard
        # (Scalars / Time Series del dashboard) en corridas nuevas.
        try:
            dt = float(self.frequency or 0)
            n_io_tick_mb = round((bytes_sent_diff + bytes_recv_diff) / BYTES_PER_MB, NET_MB_PRECISION)
            if self._t_cpu_prev is None:
                self._t_cpu_prev = float(cpu_temp) if cpu_temp else self._temp_seed
            cpu_est = predict_cpu_temp_arx(
                self._t_cpu_prev, cpu_percent, memory_percent, disk_percent, n_io_tick_mb,
                params=self._arx_cpu,
            )
            self._t_cpu_prev = cpu_est
            derived: dict[str, float] = {CPU_TEMP_EST_TAG: cpu_est}
            gpu_utils, gpu_powers, gpu_seen = await self._report_gpu_telemetry(derived)
            if not gpu_seen:
                # Fallback CPU-only: sin GPU se reutiliza U_cpu y P_cpu = f(U_cpu).
                prev = self._t_gpu_prev.get("cpu")
                if prev is None:
                    prev = float(cpu_est)
                est = predict_gpu_temp_arx(
                    prev, cpu_percent, cpu_power_from_util(cpu_percent, self._cpu_tdp),
                    0.0, 0.0, params=self._arx_gpu,
                )
                self._t_gpu_prev["cpu"] = est
                derived[GPU_TEMP_EST_TAG] = est
            u_gpu_mean = sum(gpu_utils) / len(gpu_utils) if gpu_utils else 0.0
            p_gpu_mean = sum(gpu_powers) / len(gpu_powers) if gpu_powers else 0.0
            derived[ENERGY_TOTAL_TAG] = estimate_energy_total(
                dt_s=dt,
                u_cpu_mean=cpu_percent,
                u_gpu_mean=u_gpu_mean,
                p_gpu_mean=p_gpu_mean,
                b_net_mb=n_io_tick_mb,
                weights=self._energy_w,
            )
            derived[ENERGY_EST_TAG] = estimate_energy_physical(
                p_gpu_w=p_gpu_mean, u_cpu=cpu_percent, dt_s=dt, c_base=self._base_w
            )
            self.trainer.logger.log_data(derived)
        except Exception:
            logging.exception("Error logging derived thermal/energy metrics")

    async def __maybe_checkpoint(self) -> None:
        """Sprint 6: checkpoint cada N rondas (opt-in vía checkpoint_args).

        Lee ``participant.checkpoint_args = {every_n_rounds, keep_last}``;
        ausente o <= 0 apaga el guardado. Guarda state_dict + estado del
        optimizador (best-effort) + meta con hash. Nunca interrumpe el reporte.
        """
        try:
            cp_args = (self.config.participant.get("checkpoint_args") or {})
            every = int(cp_args.get("every_n_rounds", 0) or 0)
            if every <= 0:
                return
            keep_last = int(cp_args.get("keep_last", 3) or 3)
            try:
                rnd = self.trainer.get_round()
            except Exception:
                rnd = getattr(self.trainer, "round", None)
            if rnd is None or int(rnd) <= 0 or int(rnd) % every != 0:
                return
            if self._last_checkpoint_round == int(rnd):
                return
            scenario = (self.config.participant.get("scenario_args") or {}).get("name")
            idx = (self.config.participant.get("device_args") or {}).get("idx", 0)
            tracking = self.config.participant.get("tracking_args") or {}
            config_dir = os.environ.get("NEBULA_CONFIG_DIR") or tracking.get("config_dir", "")
            try:
                state_dict = self.trainer.model.state_dict()
            except Exception:
                return
            model_info = {
                "model_class": type(getattr(self.trainer, "model", None)).__name__,
                "model_name": (self.config.participant.get("model_args") or {}).get("model"),
                "dataset": (self.config.participant.get("data_args") or {}).get("dataset"),
            }
            optim_state = None
            try:
                inner = getattr(self.trainer, "_trainer", None)
                optims = getattr(inner, "optimizers", None) if inner is not None else None
                if optims:
                    optim_state = optims[0].state_dict()
                else:
                    optim_state = getattr(getattr(self.trainer, "model", None), "_optimizer", None)
                    optim_state = optim_state.state_dict() if optim_state is not None else None
            except Exception:
                optim_state = None
            from nebula.core.checkpoint import save_checkpoint

            res = save_checkpoint(config_dir, scenario, idx, int(rnd), state_dict,
                                  model_info, optim_state, {}, keep_last)
            if res.get("ok"):
                self._last_checkpoint_round = int(rnd)
                logging.info(f"checkpoint | saved r{rnd} for node {idx} ({scenario})")
        except Exception as e:
            logging.warning(f"checkpoint | periodic save failed: {e}")

    async def _report_gpu_telemetry(self, derived: dict[str, float]) -> tuple[list[float], list[float], bool]:
        """Loguea telemetría GPU por dispositivo + su T estimada ARX.

        Devuelve (utils, powers, seen). Sin pynvml/GPUs devuelve ([], [], False)
        y el llamante aplica el fallback CPU.
        """
        gpu_utils: list[float] = []
        gpu_powers: list[float] = []
        if importlib.util.find_spec("pynvml") is None:
            return gpu_utils, gpu_powers, False
        try:
            import pynvml

            await asyncio.to_thread(pynvml.nvmlInit)
            devices = await asyncio.to_thread(pynvml.nvmlDeviceGetCount)
            for i in range(devices):
                handle = await asyncio.to_thread(pynvml.nvmlDeviceGetHandleByIndex, i)
                gpu_percent = (await asyncio.to_thread(pynvml.nvmlDeviceGetUtilizationRates, handle)).gpu
                gpu_temp = await asyncio.to_thread(
                    pynvml.nvmlDeviceGetTemperature,
                    handle,
                    pynvml.NVML_TEMPERATURE_GPU,
                )
                gpu_mem = await asyncio.to_thread(pynvml.nvmlDeviceGetMemoryInfo, handle)
                gpu_mem_percent = round(gpu_mem.used / gpu_mem.total * 100.0, NET_MB_PRECISION)
                gpu_power = await asyncio.to_thread(pynvml.nvmlDeviceGetPowerUsage, handle) / MW_TO_W
                gpu_clocks = await asyncio.to_thread(pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_SM)
                gpu_memory_clocks = await asyncio.to_thread(
                    pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_MEM
                )
                gpu_fan_speed = await asyncio.to_thread(pynvml.nvmlDeviceGetFanSpeed, handle)
                gpu_info = {
                    f"W-GPU/GPU{i} (%)": gpu_percent,
                    f"W-GPU/GPU{i} temperature (°)": gpu_temp,
                    f"W-GPU/GPU{i} memory (%)": gpu_mem_percent,
                    f"W-GPU/GPU{i} power": gpu_power,
                    f"W-GPU/GPU{i} clocks": gpu_clocks,
                    f"W-GPU/GPU{i} memory clocks": gpu_memory_clocks,
                    f"W-GPU/GPU{i} fan speed": gpu_fan_speed,
                }
                self.trainer.logger.log_data(gpu_info)
                gpu_utils.append(float(gpu_percent))
                gpu_powers.append(float(gpu_power))
                prev = self._t_gpu_prev.get(i)
                if prev is None:
                    prev = float(gpu_temp) if gpu_temp else self._temp_seed
                est = predict_gpu_temp_arx(
                    prev, gpu_percent, gpu_power, gpu_clocks, gpu_fan_speed,
                    params=self._arx_gpu,
                )
                self._t_gpu_prev[i] = est
                derived[gpu_temp_est_tag(i)] = est
        except Exception:  # noqa: S110
            pass
        return gpu_utils, gpu_powers, bool(gpu_utils)
