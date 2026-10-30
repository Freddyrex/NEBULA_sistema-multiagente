import contextlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import docker
import psutil
from dotenv import load_dotenv
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

from nebula.addons.env import check_environment
from nebula.config import defaults as _defaults
from nebula.controller.api.security.bootstrap import generated_username
from nebula.controller.controller import TermEscapeCodeFormatter
from nebula.controller.scenarios import ScenarioManagement
from nebula.utils import DockerUtils, EnvUtils, SocketUtils


def _deployer_user() -> str:
    """§3.2: sin `os.environ["USER"]` directo (KeyError en root/systemd)."""
    return _defaults.controller_name()


def _image(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _ip_suffix(name: str, default: str) -> str:
    return os.environ.get(name, default)


class NebulaEventHandler(PatternMatchingEventHandler):
    """
    Handles filesystem events for script files with extensions '.sh' and '.ps1'.

    This class monitors:
        - Creation, modification, and deletion events of shell and PowerShell scripts
          within a specified directory.

    Attributes:
        - patterns: List of file patterns to watch (e.g., '*.sh', '*.ps1').
        - last_processed: Tracks timestamps of last processed events per file to avoid duplicates.
        - timeout_ns: Minimum interval (in nanoseconds) between processing the same file.
        - processing_files: Set of files currently being processed to prevent concurrent handling.
        - lock: Threading lock to synchronize access to shared state during event handling.

    Typical use cases:
        - Automating reactions to script changes such as triggering deployments or validations.
        - Monitoring script directories for dynamic update and execution workflows.
    """

    patterns = ["*.sh", "*.ps1"]

    def __init__(self):
        super().__init__()
        self.last_processed = {}
        self.timeout_ns = 5 * 1e9
        self.processing_files = set()
        self.lock = threading.Lock()

    def _should_process_event(self, src_path: str) -> bool:
        """
        Determines whether a filesystem event for the given file path should be processed.

        This method:
            - Compares the current event timestamp with the last processed timestamp for the file.
            - Uses a timeout threshold to avoid processing duplicate or rapid consecutive events.
            - Ensures thread-safe access to the timestamp records.

        Args:
            src_path (str): The path of the file related to the event.

        Returns:
            bool: True if the event should be processed (not a duplicate within timeout), False otherwise.

        Typical use cases:
            - Debouncing rapid or duplicate filesystem events to prevent redundant processing.
        """
        current_time_ns = time.time_ns()
        print(f"Current time (ns): {current_time_ns}")
        with self.lock:
            if src_path in self.last_processed:
                print(f"Last processed time for {src_path}: {self.last_processed[src_path]}")
                last_time = self.last_processed[src_path]
                if current_time_ns - last_time < self.timeout_ns:
                    return False
            self.last_processed[src_path] = current_time_ns
        return True

    def _is_being_processed(self, src_path: str) -> bool:
        """
        Checks if a file is currently being processed to avoid concurrent handling.

        This method:
            - Uses a thread-safe lock to check and update the set of files under processing.
            - Prevents simultaneous processing of the same file by multiple threads.

        Args:
            src_path (str): The path of the file to check.

        Returns:
            bool: True if the file is already being processed, False otherwise.

        Typical use cases:
            - Ensuring serialized processing of filesystem events for the same file.
        """
        with self.lock:
            if src_path in self.processing_files:
                print(f"Skipping {src_path} as it is already being processed.")
                return True
            self.processing_files.add(src_path)
        return False

    def _processing_done(self, src_path: str):
        """
        Marks the processing of a file as completed, allowing future events for the file to be handled.

        This method:
            - Safely removes the file path from the set of currently processing files.
            - Ensures thread-safe modification of the processing state.

        Args:
            src_path (str): The path of the file whose processing is done.

        Typical use cases:
            - Signaling that a file's event handling is finished to permit subsequent processing.
        """
        with self.lock:
            if src_path in self.processing_files:
                self.processing_files.remove(src_path)

    def verify_nodes_ports(self, src_path):
        """
        Verifies and updates network ports in participant configuration files within a scenario directory.

        This method:
            - Locates participant JSON files in the scenario directory.
            - Maps current ports to new free ports starting from 50000.
            - Updates each participant's port and adjusts their neighbors' port references accordingly.
            - Saves the updated configuration back to the JSON files.
            - Handles and logs any exceptions during processing.

        Args:
            src_path (str): Path to a file within the scenario, used to determine the scenario directory.

        Typical use cases:
            - Avoiding port conflicts by dynamically reassigning ports in distributed federated learning scenarios.
            - Ensuring consistent network configuration across participant nodes.
        """
        parent_dir = os.path.dirname(src_path)
        base_dir = os.path.basename(parent_dir)
        scenario_path = os.path.join(os.path.dirname(parent_dir), base_dir)

        try:
            port_mapping = {}
            new_port_start = 50000

            participant_files = sorted(
                f for f in os.listdir(scenario_path) if f.endswith(".json") and f.startswith("participant")
            )

            for filename in participant_files:
                file_path = os.path.join(scenario_path, filename)
                with open(file_path) as json_file:
                    node = json.load(json_file)
                current_port = node["network_args"]["port"]
                port_mapping[current_port] = SocketUtils.find_free_port(start_port=new_port_start)
                print(
                    f"Participant file: {filename} | Current port: {current_port} | New port: {port_mapping[current_port]}"
                )
                new_port_start = port_mapping[current_port] + 1

            for filename in participant_files:
                file_path = os.path.join(scenario_path, filename)
                with open(file_path) as json_file:
                    node = json.load(json_file)
                current_port = node["network_args"]["port"]
                node["network_args"]["port"] = port_mapping[current_port]
                neighbors = node["network_args"]["neighbors"]

                for old_port, new_port in port_mapping.items():
                    neighbors = neighbors.replace(f":{old_port}", f":{new_port}")

                node["network_args"]["neighbors"] = neighbors

                with open(file_path, "w") as f:
                    json.dump(node, f, indent=4)

        except Exception as e:
            print(f"Error processing JSON files: {e}")

    def on_created(self, event):
        """
        Handles the creation of a file system event.

        This method:
            - Ignores directory creation events.
            - Debounces rapid or duplicate events using `_should_process_event`.
            - Prevents concurrent processing of the same file via `_is_being_processed`.
            - Logs the creation event.
            - Verifies and updates node ports based on the created file.
            - Executes the created script.
            - Marks processing completion to allow future events for the file.

        Args:
            event: The file system event object containing information about the created file.

        Typical use cases:
            - Reacting to new script files by updating network configurations and executing them automatically.
        """
        if event.is_directory:
            return
        src_path = event.src_path
        if not self._should_process_event(src_path):
            return
        if self._is_being_processed(src_path):
            return
        print("File created: %s" % src_path)
        try:
            self.verify_nodes_ports(src_path)
            self.run_script(src_path)
        finally:
            self._processing_done(src_path)

    def on_deleted(self, event):
        """
        Handles the deletion of a file system event.

        This method:
            - Ignores directory deletion events.
            - Debounces rapid or duplicate events using `_should_process_event`.
            - Prevents concurrent processing of the same file via `_is_being_processed`.
            - Logs the deletion event.
            - Attempts to kill related processes listed in 'current_scenario_pids.txt' located in the script's directory.
            - Removes the 'current_scenario_pids.txt' file after killing processes.
            - Handles and logs exceptions such as missing PID file or errors during process termination.
            - Marks processing completion to allow future events for the file.

        Args:
            event: The file system event object containing information about the deleted file.

        Typical use cases:
            - Cleaning up running processes when associated script files are removed.
        """
        if event.is_directory:
            return
        src_path = event.src_path
        if not self._should_process_event(src_path):
            return
        if self._is_being_processed(src_path):
            return
        print("File deleted: %s" % src_path)
        directory_script = os.path.dirname(src_path)
        pids_file = os.path.join(directory_script, "current_scenario_pids.txt")
        print(f"Killing processes from {pids_file}")
        try:
            self.kill_script_processes(pids_file)
            os.remove(pids_file)
        except FileNotFoundError:
            logging.warning(f"{pids_file} not found.")
        except Exception as e:
            logging.exception(f"Error while killing processes: {e}")
        finally:
            self._processing_done(src_path)

    def run_script(self, script):
        """
        Executes a given script file based on its extension.

        - Runs '.sh' scripts using bash and captures their output and errors.
        - Runs '.ps1' PowerShell scripts with execution policy bypass, launching them asynchronously.
        - Logs an error for unsupported script formats.
        - Catches and logs any exceptions during script execution.

        Args:
            script (str): Path to the script file to be executed.

        Typical use cases:
            - Automatically executing shell or PowerShell scripts triggered by file system events.
        """
        try:
            print(f"Running script: {script}")
            if script.endswith(".sh"):
                result = subprocess.Popen(["bash", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            elif script.endswith(".ps1"):
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                )
            else:
                logging.error("Unsupported script format.")
                return
        except Exception as e:
            logging.exception(f"Error while running script: {e}")

    def kill_script_processes(self, pids_file):
        """
        Forcefully terminates processes listed in a given PID file, including their child processes.

        Args:
            pids_file (str): Path to the file containing PIDs, one per line.

        Behavior:
            - Reads the PIDs from the file.
            - For each PID, checks if the process exists.
            - If it exists, kills all child processes recursively before killing the main process.
            - Handles and logs exceptions such as missing processes or invalid PID entries.
            - Logs warnings and errors appropriately.

        Typical use case:
            Used to clean up running processes related to a scenario or script that has been deleted or stopped.
        """
        try:
            with open(pids_file) as f:
                pids = f.readlines()
                for pid in pids:
                    try:
                        pid = int(pid.strip())
                        if psutil.pid_exists(pid):
                            process = psutil.Process(pid)
                            children = process.children(recursive=True)
                            print(f"Forcibly killing process {pid} and {len(children)} child processes...")
                            for child in children:
                                try:
                                    print(f"Forcibly killing child process {child.pid}")
                                    child.kill()
                                except psutil.NoSuchProcess:
                                    logging.warning(f"Child process {child.pid} already terminated.")
                                except Exception as e:
                                    logging.exception(f"Error while forcibly killing child process {child.pid}: {e}")
                            try:
                                print(f"Forcibly killing main process {pid}")
                                process.kill()
                            except psutil.NoSuchProcess:
                                logging.warning(f"Process {pid} already terminated.")
                            except Exception as e:
                                logging.exception(f"Error while forcibly killing main process {pid}: {e}")
                        else:
                            logging.warning(f"PID {pid} does not exist.")
                    except ValueError:
                        logging.exception(f"Invalid PID value in file: {pid}")
                    except Exception as e:
                        logging.exception(f"Error while forcibly killing process {pid}: {e}")
        except FileNotFoundError:
            logging.exception(f"PID file not found: {pids_file}")
        except Exception as e:
            logging.exception(f"Error while reading PIDs from file: {e}")


def run_observer():
    """
    Starts a watchdog observer to monitor the configuration directory for changes.

    This function is typically used to execute additional scripts or trigger events
    during the execution of a federated learning session by monitoring file system changes.

    Main functionalities:
        - Initializes and configures a file system observer.
        - Monitors the `/config` directory for changes.
        - Uses `NebulaEventHandler` to handle detected events.

    Typical use cases:
        - Automatically react to configuration updates.
        - Trigger specific actions during a federation lifecycle.

    Note:
        The observer runs in a blocking mode and will keep the process alive
        until manually stopped or interrupted.
    """
    # Watchdog for running additional scripts in the host machine (i.e. during the execution of a federation)
    event_handler = NebulaEventHandler()
    observer = Observer()
    config_dir = os.path.join(os.path.dirname(__file__), "/config")
    observer.schedule(event_handler, path=config_dir, recursive=True)
    observer.start()
    observer.join()


class Deployer:
    """
    Handles the configuration and initialization of deployment parameters for the NEBULA system.

    This class reads and stores various deployment-related settings such as port assignments,
    environment paths, logging configuration, and system mode (production, development, or simulation).

    Main functionalities:
        - Parses and validates input arguments for deployment.
        - Sets default values for missing parameters.
        - Detects host platform and sets up environment-specific settings.
        - Initializes the logger for deployment activities.

    Typical use cases:
        - Used to deploy the NEBULA system components with the correct configuration.
        - Enables deployment in different environments (e.g., local simulation, production, development).

    Attributes:
        - controller_port (int): Port for the main controller service.
        - waf_port (int): Port for the Web Application Firewall (WAF).
        - frontend_port (int): Port for the frontend dashboard.
        - grafana_port (int): Port for Grafana monitoring.
        - loki_port (int): Port for Loki logging service.
        - statistics_port (int): Port for the statistics service.
        - production (bool): Flag indicating if the system is in production mode.
        - dev (bool): Flag indicating if the system is in development mode.
        - advanced_analytics (bool): Enables advanced analytics modules.
        - databases_dir (str): Path to the database directory.
        - simulation (str): Simulation scenario path.
        - config_dir (str): Path to the configuration directory.
        - log_dir (str): Path to the logs directory.
        - env_path (str): Path to the Python environment.
        - root_path (str): Root directory of the NEBULA system.
        - host_platform (str): Host platform ("windows" or "unix").
        - controller_host (str): Hostname for the controller service.
        - gpu_available (bool): Indicates if a GPU is available on the host.

    Note:
        This class does not launch any services directly; it only prepares and stores configuration.
    """

    # Guarda de reentrada: evita que una segunda señal (SIGINT + SIGTERM)
    # ejecute el cleanup dos veces.
    _stopping = False

    def __init__(self, args):
        # §3.1: una sola fuente (defaults.py + env), sin literales divergentes.
        # getattr con fallback env para que argparse/env/método directo coincidan.
        def _port(attr: str, env_value: int) -> int:
            try:
                if hasattr(args, attr):
                    return int(getattr(args, attr))
            except (TypeError, ValueError):
                pass
            return int(env_value)

        self.controller_port = _port("controllerport", _defaults.CONTROLLER_PORT)
        self.waf_port = _port("wafport", _defaults.WAF_PORT)
        self.frontend_port = _port("webport", _defaults.FRONTEND_PORT)
        self.grafana_port = _port("grafanaport", _defaults.GRAFANA_PORT)
        self.loki_port = _port("lokiport", _defaults.LOKI_PORT)
        self.statistics_port = _port("statsport", _defaults.STATISTICS_PORT)
        self.production = args.production if hasattr(args, "production") else False
        self.dev = args.developement if hasattr(args, "developement") else False
        self.advanced_analytics = args.advanced_analytics if hasattr(args, "advanced_analytics") else False
        self.databases_dir = args.databases if hasattr(args, "databases") else "/nebula/app/databases"
        self.simulation = args.simulation
        self.config_dir = args.config
        self.log_dir = args.logs
        self.env_path = args.env
        self.root_path = (
            args.root_path
            if hasattr(args, "root_path")
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.host_platform = "windows" if sys.platform == "win32" else "unix"
        self.controller_host = f"{_deployer_user()}_nebula-controller"
        self.gpu_available = False
        self.admin_user = os.environ.get("NEBULA_DEFAULT_USER", "").strip()
        self.admin_password = os.environ.get("NEBULA_DEFAULT_PASSWORD", "").strip()
        self.configure_logger()
        self._load_gemini_api_key()

    def _load_gemini_api_key(self):
        """Load the Gemini API key for the SMAC cognitive layer and chatbot.

        Reads the key from the ``gemini_apikey`` (or legacy ``apikey``) file at
        the repository root (first line, stripped). When a file exists and is
        non-empty, it takes precedence and is exported as ``GEMINI_API_KEY`` so
        it propagates to the frontend, controller and node containers.
        Otherwise the environment value (if any, ``GEMINI_API_KEY`` or
        ``GOOGLE_API_KEY``) is kept. The key itself is never logged.
        """
        try:
            key = ""
            source = ""
            for name in ("gemini_apikey", "apikey"):
                apikey_path = os.path.join(self.root_path, name)
                if not os.path.isfile(apikey_path):
                    continue
                with open(apikey_path) as f:
                    key = f.readline().strip()
                if key:
                    source = name
                    break
            if key:
                os.environ["GEMINI_API_KEY"] = key
                logging.info(f"SMAC | Gemini API key loaded from '{source}' file")
            elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
                logging.info("SMAC | Gemini API key taken from environment")
            else:
                logging.info("SMAC | No Gemini API key found, cognitive layer disabled")
        except Exception as e:
            logging.warning(f"SMAC | Cannot load Gemini API key: {e}")

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resuelve las credenciales de admin que usará el controller.

        Orden de precedencia:

        1. Variables de entorno ``NEBULA_DEFAULT_USER`` / ``NEBULA_DEFAULT_PASSWORD``.
        2. Valores ya presentes en el fichero ``.env`` de la aplicación (cargado
           con ``load_dotenv`` antes de esta llamada).
        3. En desarrollo, se genera una password **y** un nombre de usuario
           aleatorios **persistentes** en el ``.env`` de la aplicación, de forma
           que las credenciales no cambian entre reinicios y el usuario puede
           verlas en ``NEBULA status``. No existe ningun usuario por defecto
           fijado en el codigo: la identidad real vive en ``users.db``.

        En producción no se genera nada: el controller aplica fail-closed y
        exige credenciales reales (ver ``nebula.controller.api.security.bootstrap``).

        Returns:
            tuple[str, str]: (usuario, password). En producción puede devolver
            cadenas vacías si el operador no las definió.
        """
        user = os.environ.get("NEBULA_DEFAULT_USER", "").strip()
        password = os.environ.get("NEBULA_DEFAULT_PASSWORD", "").strip()

        if user and password:
            return user, password

        if self.production:
            logging.warning(
                "Producción: NEBULA_DEFAULT_USER/NEBULA_DEFAULT_PASSWORD deben "
                "definirse; el controller rechazará el arranque sin ellas."
            )
            return user, password

        if not user:
            user = generated_username()
        if not password:
            password = secrets.token_urlsafe(12)

        self._persist_credentials(self.env_path, user, password)
        os.environ["NEBULA_DEFAULT_USER"] = user
        os.environ["NEBULA_DEFAULT_PASSWORD"] = password
        logging.info("Credenciales de admin de desarrollo generadas")
        return user, password

    @staticmethod
    def _persist_credentials(env_path: str, user: str, password: str, extra: dict | None = None) -> None:
        """Guarda usuario/password en el fichero ``.env`` sin duplicar claves.

        Reemplaza las líneas existentes de ``NEBULA_DEFAULT_USER`` y
        ``NEBULA_DEFAULT_PASSWORD``; si no existen, las añade al final.

        Args:
            env_path (str): Ruta del fichero ``.env`` de la aplicación.
            user (str): Usuario de admin.
            password (str): Password de admin.
            extra (dict | None): Claves adicionales a escribir (p. ej. marcas).
        """
        EnvUtils.persist_credentials(env_path, user, password, extra=extra)

    @staticmethod
    def _wait_for_http(url: str, name: str, timeout: int = 180, interval: int = 2) -> bool:
        """Espera a que un endpoint HTTP responda.

        Se usa tras crear los contenedores para no anunciar como "running"
        un servicio que todavía no escucha.

        Args:
            url (str): URL a sondear.
            name (str): Nombre legible del servicio (para los logs).
            timeout (int): Segundos máximos de espera.
            interval (int): Segundos entre intentos.

        Returns:
            bool: True si el endpoint respondió dentro del timeout.
        """
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                    if response.status < 500:
                        logging.info(f"{name} responde en {url} ({response.status})")
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    logging.info(f"{name} responde en {url} ({e.code})")
                    return True
            except Exception as e:
                logging.debug(f"{name} aún no responde ({attempt}): {e!s}")
            time.sleep(interval)

        logging.warning(f"{name} no respondió en {timeout}s: {url}")
        return False

    def configure_logger(self):
        """
        Configures the logging system for the deployment controller.

        This method sets up both console and file logging with a consistent format and appropriate log levels.
        It also ensures that Uvicorn loggers are properly configured to avoid duplicate log outputs.

        Main functionalities:
            - Defines a custom log format.
            - Sets up a stream (console) handler with INFO level.
            - Applies a terminal-specific formatter for better readability.
            - Resets and disables propagation on Uvicorn-related loggers.

        Typical use cases:
            - Enables real-time monitoring of controller events in the console.
            - Ensures clean and consistent logging output during deployment.

        Note:
            This method does not set up file logging directly, but prepares the base configuration
            and Uvicorn logger behavior for further logging use.
        """
        log_console_format = "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(TermEscapeCodeFormatter(log_console_format))
        logging.basicConfig(
            level=logging.DEBUG,
            handlers=[
                console_handler,
            ],
            force=True,
        )
        uvicorn_loggers = ["uvicorn", "uvicorn.error", "uvicorn.access"]
        for logger_name in uvicorn_loggers:
            logger = logging.getLogger(logger_name)
            logger.handlers = []  # Remove existing handlers
            logger.propagate = False  # Prevent duplicate logs

    def ensure_directory_access(self, directory_path: str) -> str:
        """
        Ensures that the specified directory exists and is writable.

        This method attempts to create the directory if it does not exist and verifies
        write access by writing and deleting a temporary metadata file.

        Args:
            directory_path (str): Path to the directory to check or create.

        Returns:
            str: Absolute path to the verified directory.

        Raises:
            SystemExit: If the directory cannot be created or write access is denied.

        Main functionalities:
            - Expands and resolves the given path.
            - Creates the directory if it doesn't exist.
            - Verifies write permissions by writing a temporary file.

        Typical use cases:
            - Validating output paths for logs, databases, or configurations before deployment.
            - Ensuring system compatibility with file permissions in production or development environments.
        """
        try:
            path = Path(os.path.expanduser(directory_path))
            path.mkdir(parents=True, exist_ok=True)

            # Write metadata file to check if directory is writable
            test_file = path / ".metadata"
            try:
                test_file.write_text("nebula")
                test_file.unlink()
            except OSError as e:
                logging.exception(f"Write permission test failed: {e!s}")
                raise SystemExit(1) from e

            logging.info(f"Successfully verified access to directory: {path}")
            return str(path.absolute())

        except Exception as e:
            logging.exception(f"Failed to create/access directory {directory_path}: {e!s}")
            logging.exception(
                "Please check directory permissions or choose a different location using --database option"
            )
            raise SystemExit(1) from e

    def start(self):
        """
        Starts the NEBULA deployment process and all associated services.

        This method initializes the NEBULA platform by setting up the environment,
        checking port availability, starting key services (controller, frontend, WAF),
        and launching a filesystem observer to react to configuration changes.

        Main functionalities:
            - Displays the NEBULA banner and authorship.
            - Loads environment variables from a file.
            - Verifies environment settings and directory access.
            - Checks and resolves port availability for all components.
            - Starts the controller, frontend, and optionally the WAF.
            - Initializes a watchdog observer to monitor the config directory.
            - Handles system signals for clean shutdown.

        Typical use cases:
            - Used to launch NEBULA in production, development, or simulation environments.
            - Central entry point for managing NEBULA components during deployment.

        Note:
            The method blocks indefinitely until manually interrupted,
            and ensures graceful shutdown upon receiving SIGINT or SIGTERM.
        """
        banner = """
                    ███╗   ██╗███████╗██████╗ ██╗   ██╗██╗      █████╗
                    ████╗  ██║██╔════╝██╔══██╗██║   ██║██║     ██╔══██╗
                    ██╔██╗ ██║█████╗  ██████╔╝██║   ██║██║     ███████║
                    ██║╚██╗██║██╔══╝  ██╔══██╗██║   ██║██║     ██╔══██║
                    ██║ ╚████║███████╗██████╔╝╚██████╔╝███████╗██║  ██║
                    ╚═╝  ╚═══╝╚══════╝╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝
                      A Platform for Decentralized Federated Learning

                      Developed by:
                       • Enrique Tomás Martínez Beltrán
                       • Alberto Huertas Celdrán
                       • Alejandro Avilés Serrano
                       • Fernando Torres Vega

                      https://nebula-dfl.com / https://nebula-dfl.eu
                """
        print("\x1b[0;36m" + banner + "\x1b[0m", flush=True)

        # Load the environment variables
        load_dotenv(self.env_path)

        # Resolve (and persist, in dev) the admin credentials used by the controller
        self.admin_user, self.admin_password = self._resolve_credentials()

        # Install the signal handlers BEFORE creating anything: a Ctrl+C during
        # the startup must clean up the containers that were already created.
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        # Check information about the environment
        check_environment()

        # Ensure database directory is accessible
        self.databases_dir = self.ensure_directory_access(self.databases_dir)

        # Save controller pid
        with open(os.path.join(os.path.dirname(__file__), "deployer.pid"), "w") as f:
            f.write(str(os.getpid()))

        try:
            self._deploy()
        finally:
            Deployer._remove_deployer_pid_file()

    def _deploy(self) -> None:
        """Creates the NEBULA containers and blocks until a termination signal.

        Separated from :meth:`start` so that the pid file is always removed and
        the containers are always cleaned up, even if the deployment fails.
        """
        # Check ports available
        if not SocketUtils.is_port_open(self.controller_port):
            self.controller_port = SocketUtils.find_free_port(start_port=self.controller_port)

        if not SocketUtils.is_port_open(self.frontend_port):
            self.frontend_port = SocketUtils.find_free_port(start_port=self.frontend_port)

        if not SocketUtils.is_port_open(self.statistics_port):
            self.statistics_port = SocketUtils.find_free_port(start_port=self.statistics_port)

        self.run_controller()
        logging.info(f"NEBULA Databases created in {self.databases_dir}")
        controller_ready = self._wait_for_http(
            f"http://127.0.0.1:{self.controller_port}/resources", "NEBULA Controller", timeout=180
        )
        if controller_ready:
            logging.info("NEBULA Controller is running")
        else:
            logging.warning(
                "NEBULA Controller no está respondiendo. Revisa: NEBULA logs controller"
            )

        self.run_frontend()
        frontend_ready = self._wait_for_http(
            f"http://127.0.0.1:{self.frontend_port}/platform", "NEBULA Frontend", timeout=180
        )
        if frontend_ready:
            logging.info(f"NEBULA Frontend is running at http://localhost:{self.frontend_port}")
        else:
            logging.warning(
                f"NEBULA Frontend no está respondiendo en http://localhost:{self.frontend_port}. "
                "Revisa: NEBULA logs frontend"
            )
        if self.production:
            self.run_waf()
            logging.info("NEBULA WAF is running")

        # Watchdog for running additional scripts in the host machine (i.e. during the execution of a federation)
        event_handler = NebulaEventHandler()
        observer = Observer()
        observer.schedule(event_handler, path=self.config_dir, recursive=True)
        observer.start()

        logging.info("NEBULA is running in the background (stop it with 'NEBULA stop')")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            observer.join(timeout=5)
            self.stop_all()
        finally:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception as e:
                logging.debug(f"No se pudo detener el watcher de forma limpia: {e!s}")

    def signal_handler(self, sig, frame):
        """
        Handles system termination signals to ensure a clean shutdown.

        This method is triggered when the application receives SIGTERM or SIGINT signals
        (e.g., via Ctrl+C or `kill`). It logs the event, performs cleanup actions, and
        terminates the process gracefully.

        Args:
            sig: Signal number received (e.g., signal.SIGINT, signal.SIGTERM).
            frame: Current stack frame at the time the signal was received.

        Main functionalities:
            - Logs the received signal.
            - Calls `self.stop_all()` to terminate running components.
            - Exits the application using `sys.exit(0)`.

        Typical use cases:
            - Ensures that NEBULA services shut down properly on manual or automated termination.
            - Prevents resource leaks or corrupted state during abrupt shutdowns.
        """
        logging.info("Received termination signal, shutting down...")
        self.stop_all()
        sys.exit(0)

    def run_frontend(self):
        """
        Runs the Nebula controller within a Docker container, ensuring the required Docker environment is available.

        This method:
            - Checks if Docker is running by verifying the Docker socket presence (platform-dependent).
            - Creates a dedicated Docker network for the Nebula system.
            - Configures environment variables, volume mounts, ports, and network settings for the container.
            - Creates and starts the Nebula controller Docker container with the specified configuration.

        Raises:
            Exception: If Docker is not running or Docker Compose is not installed.

        Typical use cases:
            - Launching the Nebula controller as part of the federated learning infrastructure.
            - Ensuring proper Docker networking and environment setup for container execution.

        Note:
            This method assumes Docker and Docker Compose are installed and accessible on the host system.
        """
        if sys.platform == "win32":
            if not os.path.exists("//./pipe/docker_Engine"):
                raise Exception(
                    "Docker is not running, please check if Docker is running and Docker Compose is installed."
                )
        else:
            if not os.path.exists("/var/run/docker.sock"):
                raise Exception(
                    "/var/run/docker.sock not found, please check if Docker is running and Docker Compose is installed."
                )

        network_name = f"{_deployer_user()}_nebula-net-base"

        # Create the Docker network
        base = DockerUtils.create_docker_network(network_name)

        client = docker.from_env()

        from nebula.controller.api.security.bootstrap import grafana_admin_password  # noqa: F401

        environment = {
            "NEBULA_CONTROLLER_NAME": _deployer_user(),
            "NEBULA_PRODUCTION": self.production,
            "NEBULA_ADVANCED_ANALYTICS": self.advanced_analytics,
            "NEBULA_FRONTEND_LOG": "/nebula/app/logs/frontend.log",
            "NEBULA_LOGS_DIR": "/nebula/app/logs/",
            "NEBULA_CONFIG_DIR": "/nebula/app/config/",
            "NEBULA_CERTS_DIR": "/nebula/app/certs/",
            "NEBULA_ENV_PATH": "/nebula/app/.env",
            "NEBULA_ROOT_HOST": self.root_path,
            "NEBULA_HOST_PLATFORM": self.host_platform,
            # §1.2: sin `admin/admin` literal; fail-closed en prod, aleatorio en dev.
            "NEBULA_DEFAULT_USER": self.admin_user,
            "NEBULA_DEFAULT_PASSWORD": self.admin_password,
            "NEBULA_CONTROLLER_PORT": self.controller_port,
            "NEBULA_CONTROLLER_HOST": self.controller_host,
            # TensorBoard corre dentro del contenedor frontend: el entrypoint lo
            # levanta en TENSORBOARD_PORT y el proxy interno del frontend debe
            # apuntar al mismo puerto; en el host se publica como statistics_port.
            "NEBULA_TENSORBOARD_PORT": str(_defaults.TENSORBOARD_PORT),
            "NEBULA_STATISTICS_PORT": str(_defaults.TENSORBOARD_PORT),
            "NEBULA_TZ": _defaults.TZ,
            # SMAC chatbot + cognitive agents: Gemini key passthrough (empty = disabled).
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        }

        volumes = ["/nebula", "/var/run/docker.sock", "/etc/nginx/sites-available/default"]

        ports = [80, _defaults.TENSORBOARD_PORT]

        host_config = client.api.create_host_config(
            binds=[
                f"{self.root_path}:/nebula",
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{self.root_path}/nebula/frontend/config/nebula:/etc/nginx/sites-available/default",
            ],
            port_bindings={80: self.frontend_port, _defaults.TENSORBOARD_PORT: self.statistics_port},
        )

        networking_config = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_FRONTEND', _defaults.IP_FRONTEND_SUFFIX)}"
            )
        })

        container_id = client.api.create_container(
            image=_image("NEBULA_IMAGE_FRONTEND", _defaults.IMAGE_FRONTEND),
            name=f"{_deployer_user()}_nebula-frontend",
            detach=True,
            environment=environment,
            volumes=volumes,
            host_config=host_config,
            networking_config=networking_config,
            ports=ports,
        )

        client.api.start(container_id)

    @staticmethod
    def stop_frontend():
        """
        Stops and removes all NEBULA frontend Docker containers associated with the current user.

        Responsibilities:
            - Detects running Docker containers with names starting with '<user>_nebula-frontend'.
            - Gracefully stops and removes these frontend containers.

        Typical use cases:
            - Cleaning up frontend containers during shutdown or redeployment processes.
        """
        DockerUtils.remove_containers_by_prefix(f"{_deployer_user()}_nebula-frontend")

    def run_controller(self):
        if sys.platform == "win32":
            if not os.path.exists("//./pipe/docker_Engine"):
                raise Exception(
                    "Docker is not running, please check if Docker is running and Docker Compose is installed."
                )
        else:
            if not os.path.exists("/var/run/docker.sock"):
                raise Exception(
                    "/var/run/docker.sock not found, please check if Docker is running and Docker Compose is installed."
                )

        network_name = f"{_deployer_user()}_nebula-net-base"

        try:
            subprocess.check_call(["nvidia-smi"])
            self.gpu_available = True
        except Exception:
            logging.info("No GPU available for the frontend, nodes will be deploy in CPU mode")

        # Create the Docker network
        base = DockerUtils.create_docker_network(network_name)

        client = docker.from_env()

        environment = {
            "USER": _deployer_user(),
            "NEBULA_CONTROLLER_NAME": _deployer_user(),
            "NEBULA_PRODUCTION": self.production,
            "NEBULA_ROOT_HOST": self.root_path,
            "NEBULA_ADVANCED_ANALYTICS": self.advanced_analytics,
            "NEBULA_DATABASES_DIR": "/nebula/app/databases",
            "NEBULA_CONTROLLER_LOG": "/nebula/app/logs/controller.log",
            "NEBULA_CONFIG_DIR": "/nebula/app/config/",
            "NEBULA_LOGS_DIR": "/nebula/app/logs/",
            "NEBULA_CERTS_DIR": "/nebula/app/certs/",
            "NEBULA_HOST_PLATFORM": self.host_platform,
            "NEBULA_CONTROLLER_PORT": self.controller_port,
            "NEBULA_CONTROLLER_HOST": self.controller_host,
            "NEBULA_FRONTEND_PORT": self.frontend_port,
            "NEBULA_TZ": _defaults.TZ,
            # Credenciales de admin: el controller crea/usa el usuario inicial
            # con estos valores (ver nebula.controller.api.security.bootstrap).
            "NEBULA_DEFAULT_USER": self.admin_user,
            "NEBULA_DEFAULT_PASSWORD": self.admin_password,
            # SMAC: passthrough so the controller can inject it into node containers.
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        }

        volumes = ["/nebula", "/var/run/docker.sock"]

        ports = [self.controller_port]

        _extra = _defaults.extra_hosts()
        host_config = client.api.create_host_config(
            binds=[
                f"{self.root_path}:/nebula",
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{self.databases_dir}:/nebula/app/databases",
            ],
            extra_hosts=_extra or None,
            port_bindings={self.controller_port: self.controller_port},
            device_requests=[
                {
                    "Driver": "nvidia",
                    "Count": -1,
                    "Capabilities": [["gpu"]],
                }
            ]
            if self.gpu_available
            else None,
        )

        networking_config = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_CONTROLLER', _defaults.IP_CONTROLLER_SUFFIX)}"
            )
        })

        container_id = client.api.create_container(
            image=_image("NEBULA_IMAGE_CONTROLLER", _defaults.IMAGE_CONTROLLER),
            name=f"{_deployer_user()}_nebula-controller",
            detach=True,
            environment=environment,
            volumes=volumes,
            host_config=host_config,
            networking_config=networking_config,
            ports=ports,
        )

        client.api.start(container_id)

    @staticmethod
    def stop_controller():
        """
        Stops all running Docker containers with names starting with '<user>_nebula-controller'.

        Responsibilities:
            - Initiates shutdown of all participant nodes related to the scenario.
            - Gracefully stops and removes controller containers to ensure clean shutdown.

        Typical use cases:
            - Used when stopping or restarting the Nebula controller service.
        """
        ScenarioManagement.stop_participants()
        DockerUtils.remove_containers_by_prefix(f"{_deployer_user()}_nebula-controller")

    def run_waf(self):
        """
        Starts and configures the Web Application Firewall (WAF) and its monitoring stack within Docker containers.

        This method:
            - Creates a user-specific Docker network for WAF-related containers.
            - Launches the 'nebula-waf' container to provide WAF functionality with log volume and port mappings.
            - Starts the 'nebula-waf-grafana' container for monitoring dashboards, configured via environment variables.
            - Launches the 'nebula-waf-loki' container for centralized log aggregation using a configuration file.
            - Starts the 'nebula-waf-promtail' container to collect and forward logs from nginx.
            - Assigns static IP addresses to all containers within the created Docker network for consistent communication.

        Typical use cases:
            - Deploying an integrated WAF solution alongside monitoring and logging components in the Nebula system.
            - Ensuring comprehensive security monitoring and log management through containerized services.
        """
        network_name = f"{_deployer_user()}_nebula-net-base"
        base = DockerUtils.create_docker_network(network_name)

        client = docker.from_env()

        volumes_waf = ["/var/log/nginx"]

        ports_waf = [80]

        host_config_waf = client.api.create_host_config(
            binds=[f"{self.log_dir}/waf/nginx:/var/log/nginx"],
            privileged=True,
            port_bindings={80: self.waf_port},
        )

        networking_config_waf = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_WAF', _defaults.IP_WAF_SUFFIX)}"
            )
        })

        container_id_waf = client.api.create_container(
            image=_image("NEBULA_IMAGE_WAF", _defaults.IMAGE_WAF),
            name=f"{_deployer_user()}_nebula-waf",
            detach=True,
            volumes=volumes_waf,
            host_config=host_config_waf,
            networking_config=networking_config_waf,
            ports=ports_waf,
        )

        client.api.start(container_id_waf)

        from nebula.controller.api.security.bootstrap import grafana_admin_password

        environment = {
            # §1.2: nunca literal `admin`; misma env que el bootstrap.
            "GF_SECURITY_ADMIN_PASSWORD": os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "")
            or os.environ.get("NEBULA_DEFAULT_PASSWORD", "")
            or grafana_admin_password(),
            "GF_USERS_ALLOW_SIGN_UP": "false",
            "GF_SERVER_HTTP_PORT": str(_defaults.GRAFANA_INTERNAL_PORT),
            "GF_SERVER_PROTOCOL": "http",
            "GF_SERVER_DOMAIN": f"localhost:{self.grafana_port}",
            "GF_SERVER_ROOT_URL": f"http://localhost:{self.grafana_port}/grafana/",
            "GF_SERVER_SERVE_FROM_SUB_PATH": "true",
            "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": "/var/lib/grafana/dashboards/dashboard.json",
            "GF_METRICS_MAX_LIMIT_TSDB": "0",
        }

        ports = [_defaults.GRAFANA_INTERNAL_PORT]

        host_config = client.api.create_host_config(
            port_bindings={_defaults.GRAFANA_INTERNAL_PORT: self.grafana_port},
        )

        networking_config = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_GRAFANA', _defaults.IP_GRAFANA_SUFFIX)}"
            )
        })

        container_id = client.api.create_container(
            image=_image("NEBULA_IMAGE_GRAFANA", _defaults.IMAGE_GRAFANA),
            name=f"{_deployer_user()}_nebula-waf-grafana",
            detach=True,
            environment=environment,
            host_config=host_config,
            networking_config=networking_config,
            ports=ports,
        )

        client.api.start(container_id)

        command = ["-config.file=/mnt/config/loki-config.yml"]

        ports_loki = [_defaults.LOKI_PORT]

        host_config_loki = client.api.create_host_config(
            port_bindings={_defaults.LOKI_PORT: self.loki_port},
        )

        networking_config_loki = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_LOKI', _defaults.IP_LOKI_SUFFIX)}"
            )
        })

        container_id_loki = client.api.create_container(
            image=_image("NEBULA_IMAGE_LOKI", _defaults.IMAGE_LOKI),
            name=f"{_deployer_user()}_nebula-waf-loki",
            detach=True,
            command=command,
            host_config=host_config_loki,
            networking_config=networking_config_loki,
            ports=ports_loki,
        )

        client.api.start(container_id_loki)

        volumes_promtail = ["/var/log/nginx"]

        host_config_promtail = client.api.create_host_config(
            binds=[
                f"{self.log_dir}/waf/nginx:/var/log/nginx",
            ],
        )

        networking_config_promtail = client.api.create_networking_config({
            f"{network_name}": client.api.create_endpoint_config(
                ipv4_address=f"{base}{_ip_suffix('NEBULA_IP_PROMTAIL', _defaults.IP_PROMTAIL_SUFFIX)}"
            )
        })

        container_id_promtail = client.api.create_container(
            image=_image("NEBULA_IMAGE_PROMTAIL", _defaults.IMAGE_PROMTAIL),
            name=f"{_deployer_user()}_nebula-waf-promtail",
            detach=True,
            volumes=volumes_promtail,
            host_config=host_config_promtail,
            networking_config=networking_config_promtail,
        )

        client.api.start(container_id_promtail)

    @staticmethod
    def stop_waf():
        """
        Stops all running Docker containers with names starting with '<user>_nebula-waf'.

        Responsibilities:
            - Gracefully shuts down and removes all WAF-related containers for the current user.

        Typical use cases:
            - Cleaning up WAF containers during shutdown or redeployment of the Nebula system.
        """
        DockerUtils.remove_containers_by_prefix(f"{_deployer_user()}_nebula-waf")

    @staticmethod
    def stop_all():
        """
        Stops all running Nebula-related Docker containers and networks.

        Responsibilities:
            - Stops frontend, controller, and WAF containers for the current user.
            - Removes all Docker containers and networks with names starting with the user's prefix.
            - Terminates the deployer process registered in the PID file (only if it
              really is a NEBULA deployer: never kills an unrelated process).
            - Exits the system cleanly, handling any exceptions during shutdown.

        Note:
            The deployer never sends SIGKILL to itself: the process used to die
            with SIGKILL, which made the shell print "killed" instead of a clean
            exit. Now the caller exits normally with status 0.

        Typical use cases:
            - Full shutdown and cleanup of all Nebula components and resources on the host system.
        """
        if Deployer._stopping:
            sys.exit(0)
        Deployer._stopping = True

        print("Closing NEBULA (exiting from components)... Please wait")
        errors = []
        for name, stopper in (
            ("frontend", Deployer.stop_frontend),
            ("controller", Deployer.stop_controller),
            ("waf", Deployer.stop_waf),
        ):
            try:
                stopper()
            except Exception as e:
                errors.append(f"{name}: {e}")

        try:
            DockerUtils.remove_containers_by_prefix(f"{_deployer_user()}_")
            DockerUtils.remove_docker_networks_by_prefix(f"{_deployer_user()}_")
        except Exception as e:
            errors.append(f"cleanup: {e}")

        Deployer._terminate_deployer_process()

        if errors:
            print(f"Nebula is closed with errors: {'; '.join(errors)}")
        sys.exit(0)

    @staticmethod
    def set_admin_password(password: str | None, databases_dir: str) -> int:
        """Fija (o muestra) las credenciales de admin de la plataforma.

        Actualiza la base de datos de usuarios (``users.db``) y persiste las
        credenciales en el ``.env`` de la aplicación, de forma que los
        siguientes arranques reutilicen las mismas y el login funcione.

        La base de datos la crea el contenedor del controller como ``root``,
        asi que desde el host puede no ser escribible. En ese caso el cambio
        se aplica dentro del contenedor y, si no esta levantado, se guarda en
        el ``.env`` para que se aplique en el proximo arranque.

        Args:
            password (str | None): Nueva password. Si es ``None``, solo se
                muestran las credenciales configuradas actualmente.
            databases_dir (str): Directorio donde vive ``users.db``.

        Returns:
            int: 0 si la operación fue correcta, 1 si hubo un error.
        """
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        configured_user = ""
        configured_password = ""
        if os.path.isfile(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("NEBULA_DEFAULT_USER="):
                        configured_user = line.split("=", 1)[1].strip()
                    elif line.startswith("NEBULA_DEFAULT_PASSWORD="):
                        configured_password = line.split("=", 1)[1].strip()

        # La base de datos manda: el administrador es el primer usuario
        # registrado. El .env es solo el espejo/configuracion de arranque.
        user = Deployer._resolve_platform_admin(databases_dir) or configured_user
        if not user:
            user = os.environ.get("NEBULA_DEFAULT_USER", "").strip()

        if not password:
            print(f"Usuario:  {configured_user or user or '(sin definir)'}")
            print(f"Password: {configured_password or '(sin definir; se genera en el proximo arranque)'}")
            return 0

        try:
            updated = Deployer._set_password_on_host(user, password, databases_dir)
        except Exception as e:
            logging.info(f"No se pudo escribir la BD desde el host: {e!s}")
            if Deployer._set_password_in_controller(user, password):
                Deployer._persist_credentials(env_path, user, password)
                print(f"Password de admin actualizada para: {user} (dentro del contenedor)")
                print(f"Usuario: {user}")
                return 0

            Deployer._persist_credentials(env_path, user, password, extra={"NEBULA_ADMIN_PASSWORD_PENDING": "1"})
            print(f"Password guardada en {env_path}.")
            print("Se aplicara a la base de datos en el proximo arranque de NEBULA.")
            print(f"Usuario: {user}")
        else:
            Deployer._persist_credentials(env_path, user, password)
            print(f"Password de admin actualizada para: {updated}")
            print(f"Usuario: {user}")
        return 0

    @staticmethod
    def _resolve_platform_admin(databases_dir: str) -> str:
        """Devuelve el administrador de la plataforma segun ``users.db``.

        Args:
            databases_dir (str): Directorio donde vive ``users.db``.

        Returns:
            str: Nombre del administrador, o "" si no se pudo determinar.
        """
        try:
            from nebula.controller import database as _database

            _database.user_db_file_location = os.path.join(databases_dir, "users.db")
            admin = _database.get_platform_admin()
            return admin["user"] if admin else ""
        except Exception:
            return ""

    @staticmethod
    def _set_password_on_host(user: str, password: str, databases_dir: str) -> str:
        """Actualiza la password del administrador directamente sobre ``users.db``.

        El administrador se resuelve en la base de datos (primer usuario
        registrado con rol admin); no hay ningun nombre fijo en el codigo.

        Args:
            user (str): Nombre de usuario a crear si no hay usuarios.
            password (str): Nueva password.
            databases_dir (str): Directorio donde vive ``users.db``.

        Returns:
            str: Nombres de los usuarios actualizados.

        Raises:
            Exception: Si la base de datos no se puede escribir desde el host.
        """
        from nebula.controller import database as _database

        _database.user_db_file_location = os.path.join(databases_dir, "users.db")
        admin = _database.get_platform_admin()

        if admin:
            _database.update_user(admin["user"], password, admin.get("role") or "admin")
            return admin["user"]

        _database.add_user(user, password, "admin")
        return user

    @staticmethod
    def _set_password_in_controller(user: str, password: str) -> bool:
        """Aplica la password de admin dentro del contenedor del controller.

        La base de datos ``users.db`` la crea el contenedor como ``root``, por
        lo que el usuario del host puede no tener permiso de escritura.

        Args:
            user (str): Nombre de usuario (se normaliza a mayusculas).
            password (str): Nueva password.

        Returns:
            bool: True si la password se aplico en la base de datos.
        """
        if not re.fullmatch(r"^[A-Za-z0-9._-]{1,64}$", user):
            return False

        container = f"{_deployer_user()}_nebula-controller"
        script = (
            "import os,sys;"
            "sys.path.insert(0,'/nebula');"
            "from nebula.controller import database as db;"
            "db.user_db_file_location='/nebula/app/databases/users.db';"
            "pwd=sys.stdin.read().strip();"
            "admin=db.get_platform_admin();"
            "usr=(admin['user'] if admin else os.environ.get('NEBULA_NEW_USER','').strip().upper());"
            "info=db.get_user_info(usr) if usr else None;"
            "db.update_user(usr,pwd,(info['role'] or 'admin')) if info else db.add_user(usr,pwd,'admin');"
            "print('ok')"
        )
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "-e",
                    f"NEBULA_NEW_USER={user}",
                    container,
                    "/.venv/bin/python",
                    "-c",
                    script,
                ],
                input=password,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and "ok" in (result.stdout or "")

    @staticmethod
    def _deployer_pid() -> int | None:
        """Devuelve el PID registrado en ``deployer.pid`` (o None si no hay)."""
        pid_file = os.path.join(os.path.dirname(__file__), "deployer.pid")
        try:
            with open(pid_file) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _remove_deployer_pid_file() -> None:
        """Elimina ``deployer.pid`` si existe (idempotente)."""
        pid_file = os.path.join(os.path.dirname(__file__), "deployer.pid")
        with contextlib.suppress(OSError):
            os.remove(pid_file)

    @staticmethod
    def _terminate_deployer_process() -> None:
        """Termina el deployer registrado en el PID file si es otro proceso.

        Nunca envía una señal a un PID ajeno: se comprueba con psutil que la
        línea de comandos corresponde al deployer de NEBULA. Si el PID es el
        del proceso actual, simplemente se borra el fichero de PID (el proceso
        ya está termine él mismo con ``sys.exit(0)``).
        """
        pid = Deployer._deployer_pid()
        Deployer._remove_deployer_pid_file()

        if not pid or pid == os.getpid():
            return

        try:
            process = psutil.Process(pid)
            cmdline = " ".join(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return

        if "main.py" not in cmdline:
            logging.warning(f"El PID {pid} no pertenece a un deployer de NEBULA; no se termina.")
            return

        logging.info(f"Terminando el deployer de NEBULA (PID {pid})")
        try:
            process.terminate()
            process.wait(timeout=30)
        except psutil.TimeoutExpired:
            logging.warning(f"El deployer (PID {pid}) no respondió a SIGTERM; se fuerza la parada.")
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
