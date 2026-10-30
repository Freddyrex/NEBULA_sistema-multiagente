#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
import hmac
import io
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import zipfile

from flask import (
    Flask,
    abort,
    jsonify,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

from nebula.config.defaults import PHYSICAL_NODE_PORT

# ──────────────────────────────  GLOBAL CONFIG  ──────────────────────────────

app = Flask(__name__)

# §3.5: dirs absolutos por defecto (env override); nunca relativos ./app.
CERTS_FOLDER = os.environ.get("PHYSICAL_CERTS_DIR", os.environ.get("NEBULA_PHYSICAL_CERTS_DIR", "/nebula/app/certs"))
CONFIG_FOLDER = os.environ.get("PHYSICAL_CONFIG_DIR", os.environ.get("NEBULA_PHYSICAL_CONFIG_DIR", "/nebula/app/config"))
LOGS_FOLDER = os.environ.get("PHYSICAL_LOGS_DIR", os.environ.get("NEBULA_PHYSICAL_LOGS_DIR", "/nebula/app/logs"))
METRICS_FOLDER = os.environ.get("PHYSICAL_METRICS_DIR", os.environ.get("NEBULA_PHYSICAL_METRICS_DIR", "/nebula/app/logs/metrics"))
NODE_ENTRYPOINT = os.environ.get("NEBULA_NODE_ENTRYPOINT", "/nebula/nebula/core/node.py")

#: Timeout (s) of the TCP reachability probe against the neighbour nodes.
NODE_REACHABILITY_TIMEOUT_S = float(os.environ.get("NEBULA_NODE_REACHABILITY_TIMEOUT_S", "3"))

#: Whether neighbour IPs are probed over TCP before starting a run.
NODE_TCP_CHECK = os.environ.get("NEBULA_NODE_TCP_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")

#: Strict allow-list for the scenario/config folder segment of a request.
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Emitted once when the API runs without a configured bearer token.
AUTH_WARNING_EMITTED = False

CONFIG_FILE_COUNT  = 1          # Exactly one *.json* per run
DATASET_FILE_COUNT = 2          # Exactly two *.h5* per run

# Current training subprocess (None ⇢ not running)
TRAINING_PROC: subprocess.Popen | None = None

# ──────────────────────────────  HELPER ROUTINES  ────────────────────────────
def _find_x_files(folder: str, extension: str = ".json") -> list[str]:
    """
    Return *all* files inside *folder* ending with *extension*.

    The check is non-recursive on purpose – the project stores every run in a
    dedicated directory with a flat layout.
    """
    return [
        os.path.join(folder, fn)
        for fn in os.listdir(folder)
        if fn.endswith(extension)
    ]


def host_part(address: str) -> str:
    """
    Return the host component of an ``ip:port`` address.

    Args:
        address (str): The address to split.

    Returns:
        str: The host part of *address*.
    """
    value = str(address or "").strip()
    return value.rsplit(":", 1)[0] if ":" in value else value


def load_node_registry() -> list[str]:
    """
    Load the static node registry used to validate neighbour addresses.

    The registry comes from ``NEBULA_NODES`` (space or comma separated
    ``ip[:port]`` entries) or from ``NEBULA_NODE_REGISTRY_FILE`` (JSON list of
    addresses or of objects carrying an ``address``/``ip``/``host`` key).

    Returns:
        list[str]: The registered addresses, empty when no registry is set.
    """
    raw = os.environ.get("NEBULA_NODES", "").strip()
    if raw:
        return [entry for entry in re.split(r"[\s,]+", raw) if entry]

    registry_file = os.environ.get("NEBULA_NODE_REGISTRY_FILE", "").strip()
    if not registry_file or not os.path.isfile(registry_file):
        return []

    try:
        with open(registry_file) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []

    if isinstance(data, dict):
        data = data.get("nodes", [])
    if not isinstance(data, list):
        return []

    entries: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            entries.append(item.strip())
        elif isinstance(item, dict):
            address = item.get("address") or item.get("ip") or item.get("host")
            if address:
                entries.append(str(address))
    return entries


def node_ips() -> set[str]:
    """
    Return the host parts declared in the static node registry.

    Returns:
        set[str]: The registered host IPs.
    """
    return {host_part(entry) for entry in load_node_registry() if entry}


def _LFI_sentry(path: str) -> bool:
    """
    Very strict Local-File-Inclusion guard.

    Returns **True** when *path* is unsafe (attempted path traversal or the
    referenced folder does not exist), **False** otherwise.
    """
    forbidden_tokens = (
        "", "..", "/", "\\", "~", "*", "?", ":", "<", ">", "|", '"', "'", "`",
        "$", "%", "&", "!", "{", "}", "[", "]", "@", "#", "+", "=", ";", ",",
        " ", "\t", "\n", "\r", "\f", "\v",
    )
    return (
        not os.path.exists(os.path.join(CONFIG_FOLDER, path))
        or any(tok in path for tok in forbidden_tokens)
    )


def _json_abort(code: int, msg: str) -> None:
    """Abort the current request emitting a JSON payload: `{"detail": msg}`."""
    response = jsonify({"detail": msg})
    response.status_code = code
    abort(response)


def _requested_path() -> str:
    """
    Return the scenario/config folder name of the request.

    The segment is validated against a strict allow-list, so it can never
    carry separators, traversal tokens or absolute paths.

    Returns:
        str: The validated folder name.

    Raises:
        werkzeug.exceptions.HTTPException: 400 when the segment is invalid.
    """
    path = request.args.get("path", "")
    if not PATH_SEGMENT_PATTERN.match(path or ""):
        _json_abort(400, "Invalid path")
    return path


def _safe_upload_name(filename: str) -> str:
    """
    Return a sanitized upload filename that is safe to join with a folder.

    Path-like names are rejected instead of being rewritten, so a traversal
    attempt never reaches the filesystem.

    Args:
        filename (str): The raw filename sent by the client.

    Returns:
        str: The sanitized filename.

    Raises:
        werkzeug.exceptions.HTTPException: 400 when the name is empty or
            carries path separators.
    """
    raw = (filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
        _json_abort(400, "Invalid filename")

    safe = secure_filename(raw)
    if not safe:
        _json_abort(400, "Invalid filename")
    return safe


@app.before_request
def _require_bearer_token():
    """
    Enforce bearer-token authentication on every route.

    The expected token is read from ``NEBULA_PHYSICAL_TOKEN``. When the
    variable is unset the service runs in development mode: the request is
    allowed and a warning is logged once. The token is never logged.

    Returns:
        tuple | None: A 401 response when the token is missing or invalid.
    """
    global AUTH_WARNING_EMITTED

    expected = os.environ.get("NEBULA_PHYSICAL_TOKEN", "")
    if not expected:
        if not AUTH_WARNING_EMITTED:
            AUTH_WARNING_EMITTED = True
            logging.warning(
                "NEBULA_PHYSICAL_TOKEN is not set: the physical node API runs unauthenticated (development mode)"
            )
        return None

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        return jsonify({"detail": "Unauthorized"}), 401
    return None

# ────────────────────────────────  END-POINTS  ───────────────────────────────
# ———————————————————————————————  CONFIG  ————————————————————————————————

@app.route("/config/", methods=["GET"])
def get_config():
    """Return the single *.json* config file for the requested run."""
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    json_files = _find_x_files(os.path.join(CONFIG_FOLDER, path))
    if len(json_files) != CONFIG_FILE_COUNT:
        _json_abort(404, "Item not found")

    return send_file(json_files.pop(), mimetype="application/json",
                     as_attachment=True)


@app.route("/config/", methods=["PUT"])
def set_config():
    """Upload a config *.json* for the provided run directory."""
    if "config" not in request.files:
        _json_abort(400, "Missing file field 'config'")

    filename = _safe_upload_name(request.files["config"].filename)

    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    os.makedirs(os.path.join(CONFIG_FOLDER, path), exist_ok=True)

    uploaded = request.files["config"]
    dst = os.path.join(CONFIG_FOLDER, path, filename)
    uploaded.save(dst)

    return jsonify(filename=filename), 201


@app.route("/config/", methods=["DELETE"])
def delete_config():
    """Remove the config *.json* from the given run directory."""
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    json_files = _find_x_files(os.path.join(CONFIG_FOLDER, path))
    if len(json_files) != CONFIG_FILE_COUNT:
        _json_abort(404, "Item not found")

    fn = json_files.pop()
    os.remove(fn)
    return jsonify(filename=fn)

# ———————————————————————————————  DATASET  ————————————————————————————————

@app.route("/dataset/", methods=["GET"])
def get_dataset():
    """
    Deliver both *.h5* datasets as a single ZIP archive.

    Returning a single payload simplifies transfer, cache-control and client
    code compared to sending two independent responses.
    """
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    h5_files = _find_x_files(os.path.join(CONFIG_FOLDER, path), ".h5")
    if len(h5_files) != DATASET_FILE_COUNT:
        _json_abort(404, "Item not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in h5_files:
            zf.write(f, arcname=os.path.basename(f))
    buf.seek(0)

    return send_file(buf, mimetype="application/zip",
                     download_name="dataset.zip", as_attachment=True)


@app.route("/dataset/", methods=["PUT"])
def set_dataset():
    """Upload the pair of train/test *.h5* files for a run."""
    fields = ("dataset", "dataset_p")
    missing = [fld for fld in fields if fld not in request.files]
    if missing:
        _json_abort(400, f"Missing file field(s): {', '.join(missing)}")

    filenames = [_safe_upload_name(request.files[fld].filename) for fld in fields]

    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    os.makedirs(os.path.join(CONFIG_FOLDER, path), exist_ok=True)
    stored: list[str] = []

    for fld, filename in zip(fields, filenames, strict=True):
        up = request.files[fld]
        dst = os.path.join(CONFIG_FOLDER, path, filename)
        up.save(dst)
        stored.append(filename)

    return jsonify(stored), 201


@app.route("/dataset/", methods=["DELETE"])
def delete_dataset():
    """Delete both dataset *.h5* files from the specified run directory."""
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    data_files = _find_x_files(os.path.join(CONFIG_FOLDER, path), ".h5")
    if len(data_files) != DATASET_FILE_COUNT:
        _json_abort(404, "Item not found")

    removed: dict[str, str] = {}
    for fn in data_files:
        os.remove(fn)
        removed[fn] = "deleted"
    return jsonify(removed)

# ————————————————————————————————  CERTS  ————————————————————————————————

@app.route("/certs/", methods=["GET"])
def get_certs():
    """Download every *.cert* file in a ZIP archive."""
    certs_files = _find_x_files(CERTS_FOLDER, ".cert")
    if not certs_files:
        _json_abort(404, "No cert files found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in certs_files:
            zf.write(f, arcname=os.path.basename(f))
    buf.seek(0)

    return send_file(buf, mimetype="application/zip",
                     download_name="certs.zip", as_attachment=True)


@app.route("/certs/", methods=["PUT"])
def set_cert():
    """Upload one *.cert* file to the global certificates folder."""
    if "cert" not in request.files:
        _json_abort(400, "Missing file field 'cert'")

    filename = _safe_upload_name(request.files["cert"].filename)

    dst = os.path.join(CERTS_FOLDER, filename)
    request.files["cert"].save(dst)

    return jsonify(filename=filename), 201


@app.route("/certs/", methods=["DELETE"])
def delete_certs():
    """Remove **all** certificate files from the system."""
    certs_files = _find_x_files(CERTS_FOLDER, ".cert")
    removed: dict[str, str] = {}
    for fn in certs_files:
        os.remove(fn)
        removed[fn] = "deleted"
    return jsonify(removed)

# ————————————————————————————————  LOGS  ————————————————————————————————

def _send_single_log(path: str, pattern: str):
    """Return the shortest matching log file inside *path*."""
    log_files = _find_x_files(path, pattern)
    if not log_files:
        _json_abort(404, "Log file not found")

    target = min(log_files, key=lambda x: len(os.path.basename(x)))
    return send_file(target, mimetype="text/plain", as_attachment=True)

@app.route("/get_logs/", methods=["GET"])
def get_logs():
    """Download the main *.log* produced during training."""
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")
    return _send_single_log(os.path.join(LOGS_FOLDER, path), ".log")


@app.route("/get_logs/", methods=["DELETE"])
def delete_logs():
    """Delete the main *.log* for the requested run."""
    path = _requested_path()
    if _LFI_sentry(path):
        _json_abort(404, "Item not found")

    log_files = _find_x_files(os.path.join(LOGS_FOLDER, path), ".log")
    if not log_files:
        _json_abort(404, "Log file not found")

    target = min(log_files, key=lambda x: len(os.path.basename(x)))
    os.remove(target)
    return jsonify(filename=target)


# Create *dedicated* GET+DELETE routes for debug/error/training logs
def _log_route(route_name: str, filename: str) -> None:
    """
    Register two URL rules:

      • **GET  /get_logs/<route_name>/**  → download the file
      • **DELETE /get_logs/<route_name>/** → delete the file
    """
    def _getter():
        path = _requested_path()
        if _LFI_sentry(path):
            _json_abort(404, "Item not found")
        return _send_single_log(os.path.join(LOGS_FOLDER, path), filename)

    def _deleter():
        path = _requested_path()
        if _LFI_sentry(path):
            _json_abort(404, "Item not found")

        files = _find_x_files(os.path.join(LOGS_FOLDER, path), filename)
        if not files:
            _json_abort(404, "Log file not found")

        target = files.pop()
        os.remove(target)
        return jsonify(filename=target)

    # `endpoint` must be unique – build one from the route name + HTTP verb
    app.add_url_rule(
        f"/get_logs/{route_name}/",
        endpoint=f"get_logs_{route_name}_get",
        view_func=_getter,
        methods=["GET"],
    )
    app.add_url_rule(
        f"/get_logs/{route_name}/",
        endpoint=f"get_logs_{route_name}_del",
        view_func=_deleter,
        methods=["DELETE"],
    )

for _name in ("debug", "error", "training"):
    _log_route(_name, f"{_name}.log")

# ————————————————————————————————  METRICS  ————————————————————————————————

@app.route("/metrics/", methods=["GET"])
def get_metrics():
    """Bundle every file under *METRICS_FOLDER* into a ZIP archive."""
    log_files = _find_x_files(METRICS_FOLDER, "")
    if not log_files:
        _json_abort(404, "Log file not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in log_files:
            zf.write(f, arcname=os.path.basename(f))
    buf.seek(0)

    return send_file(buf, mimetype="application/zip",
                     download_name="metrics.zip", as_attachment=True)

# ————————————————————————————————  ACTIONS  ————————————————————————————————

@app.route("/run/", methods=["GET"])
def run():
    """
    Spawn the federated training process (once).

    Returns
    -------
    JSON {pid, state}
    """
    json_files = _find_x_files(CONFIG_FOLDER)
    if len(json_files) != CONFIG_FILE_COUNT:
        _json_abort(404, "Config file not found")

    global TRAINING_PROC
    if TRAINING_PROC and TRAINING_PROC.poll() is None:
        _json_abort(409, "Training already running")

    cmd = [sys.executable, NODE_ENTRYPOINT, json_files[0]]
    TRAINING_PROC = subprocess.Popen(cmd)

    return jsonify(pid=TRAINING_PROC.pid, state="running")


@app.route("/stop/", methods=["GET"])
def stop():
    """Terminate the running training process (SIGTERM) and wait for it."""
    global TRAINING_PROC
    if not TRAINING_PROC or TRAINING_PROC.poll() is not None:
        _json_abort(404, "No training running")

    TRAINING_PROC.send_signal(signal.SIGTERM)
    TRAINING_PROC.wait()
    pid = TRAINING_PROC.pid
    TRAINING_PROC = None

    return jsonify(pid=pid, state="stopped")

# ———————————————————————  SETUP – UPLOAD 3 FILES  ————————————————————————

@app.route("/setup/", methods=["PUT"])
def setup_new_run():
    """
    Prepare a **new** federated-learning round.

    Expected multipart-form fields
    -------------------------------
    * **config**     – JSON with scenario, network and security arguments
    * **global_test** – shared evaluation dataset (`*.h5`)
    * **train_set**   – participant-specific training dataset (`*.h5`)

    The function rewrites paths inside *config*, validates neighbour IPs
    through Tailscale, deletes previous artefacts and finally stores the new
    trio of files.
    """
    # 1 · Refuse while a training task is still running
    global TRAINING_PROC
    if TRAINING_PROC and TRAINING_PROC.poll() is None:
        _json_abort(409, "Training already running; pause or stop it first.")

    # 2 · Check field presence
    missing = [x for x in ("config", "global_test", "train_set")
               if x not in request.files]
    if missing:
        _json_abort(400, f"Missing file field(s): {', '.join(missing)}")

    config_up   = request.files["config"]
    global_test = request.files["global_test"]
    train_set   = request.files["train_set"]

    config_name      = _safe_upload_name(config_up.filename)
    global_test_name = _safe_upload_name(global_test.filename)
    train_set_name   = _safe_upload_name(train_set.filename)

    # 3 · Extension sanity
    if not config_name.endswith(".json"):
        _json_abort(400, f"`{config_name}` must have a .json extension.")
    for name in (global_test_name, train_set_name):
        if not name.endswith(".h5"):
            _json_abort(400, f"`{name}` must have a .h5 extension.")

    # 4 · Parse + patch JSON
    try:
        original_cfg = json.load(config_up)
    except Exception as exc:  # broad – any parsing failure should abort
        _json_abort(400, f"Invalid JSON file: {exc}")

    # Update tracking / security paths to local folders
    tracking = original_cfg.get("tracking_args", {})
    tracking["log_dir"]    = LOGS_FOLDER.rstrip("/")
    tracking["config_dir"] = CONFIG_FOLDER.rstrip("/")
    original_cfg["tracking_args"] = tracking

    sec = original_cfg.get("security_args", {})
    for key in ("certfile", "keyfile", "cafile"):
        if key in sec and sec[key]:
            sec[key] = os.path.join(CERTS_FOLDER.rstrip("/"),
                                    os.path.basename(sec[key]))
    original_cfg["security_args"] = sec

    # 5 · Validate neighbour reachability against the static node registry
    neigh_str = original_cfg.get("network_args", {}).get("neighbors", "").strip()
    requested_ips: set[str] = {host_part(n) for n in neigh_str.split() if n}

    if requested_ips:
        registry = load_node_registry()
        if not registry:
            _json_abort(
                400,
                "Node registry is empty: set NEBULA_NODES or "
                "NEBULA_NODE_REGISTRY_FILE to validate neighbours.",
            )
        known_ips = set(node_ips())
        missing = sorted(ip for ip in requested_ips if ip not in known_ips)
        if missing:
            _json_abort(
                400,
                f"Neighbour IP(s) not in node registry: {', '.join(missing)}",
            )
        if NODE_TCP_CHECK:
            for ip in sorted(requested_ips):
                try:
                    with socket.create_connection(
                        (ip, PHYSICAL_NODE_PORT),
                        timeout=NODE_REACHABILITY_TIMEOUT_S,
                    ):
                        pass
                except OSError as exc:
                    _json_abort(
                        400,
                        f"Neighbour {ip}:{PHYSICAL_NODE_PORT} not reachable: {exc}",
                    )

    # 6 · Clean previous JSON/H5 artefacts
    for fn in os.listdir(CONFIG_FOLDER):
        if fn.endswith((".json", ".h5")):
            try:
                os.remove(os.path.join(CONFIG_FOLDER, fn))
            except OSError:
                pass
    if any(fn.endswith((".json", ".h5")) for fn in os.listdir(CONFIG_FOLDER)):
        _json_abort(400, "Could not delete old JSON/H5 files.")

    # 7 · Persist patched JSON
    json_dest = os.path.join(CONFIG_FOLDER, config_name)
    with open(json_dest, "wb") as dst:
        dst.write(json.dumps(original_cfg, indent=2).encode())

    # 8 · Persist datasets
    saved = [config_name]
    for up, name in ((global_test, global_test_name), (train_set, train_set_name)):
        dst = os.path.join(CONFIG_FOLDER, name)
        up.save(dst)
        saved.append(name)

    # 9 · Purge previous log files
    for root, _, files in os.walk(LOGS_FOLDER):
        for fn in files:
            if fn.endswith(".log"):
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass
    if any(fn.endswith(".log") for _, _, fns in os.walk(LOGS_FOLDER) for fn in fns):
        _json_abort(400, "Could not delete old log files.")

    return jsonify(saved), 201

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY-POINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Local testing:  python main.py (§3.1: host/puerto por env, no literales).
    app.run(
        host=os.environ.get("NEBULA_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PHYSICAL_PORT", os.environ.get("PHYSICAL_NODE_PORT", 8000))),
        debug=False,
    )
