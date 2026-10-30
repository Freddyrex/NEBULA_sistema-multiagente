"""Node checkpoints (Sprint 6, ROADMAP_AGENTE_ADAPTATIVO).

Per-node, per-round weight snapshots that answer "can we continue if the
architecture changes?" with data and mechanism. Layout (under the scenario dir,
next to operator_commands/ so frontend and nodes share it via bind-mount)::

    <config_dir>/<scenario>/checkpoints/participant_<idx>/r<round>/
        weights.pt  (torch.save of the model state_dict)
        optim.pt    (optional optimizer state_dict, when obtainable post-fit)
        meta.json   (round, model class/name, dataset, shapes, sha256,
                     config snapshot, metric finals, optimizer flag)

Rules: torch stays inside functions (cheap module import); every function is
best-effort except ``load_weights``/``apply_resume``, which raise ValueError
with an explicit message on corrupt/incompatible checkpoints so a node never
starts silently on bad state (acceptance: explicit failure, no boot).
``validate()`` never raises: (ok, reason).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

CHECKPOINTS_DIRNAME = "checkpoints"
WEIGHTS_FILE = "weights.pt"
OPTIM_FILE = "optim.pt"
META_FILE = "meta.json"


def checkpoints_root(config_dir: str | None, scenario: str | None) -> str | None:
    """``<config_dir>/<scenario>/checkpoints`` (None when unresolvable)."""
    try:
        if not config_dir or not scenario or scenario in (".", ".."):
            return None
        return os.path.join(config_dir, scenario, CHECKPOINTS_DIRNAME)
    except Exception:
        return None


def checkpoint_dir(config_dir: str | None, scenario: str | None, node_idx: Any, round_: Any) -> str | None:
    try:
        root = checkpoints_root(config_dir, scenario)
        if root is None:
            return None
        return os.path.join(root, f"participant_{node_idx}", f"r{int(round_)}")
    except Exception:
        return None


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def save_checkpoint(
    config_dir: str | None,
    scenario: str | None,
    node_idx: Any,
    round_: Any,
    state_dict: Any,
    model_info: dict | None = None,
    optimizer_state: Any | None = None,
    metrics: dict | None = None,
    keep_last: int = 3,
) -> dict[str, Any]:
    """Persist weights (+optional optimizer state) with integrity metadata.

    Returns ``{ok, dir|reason}``. Prunes older round dirs beyond ``keep_last``.
    Never raises.
    """
    try:
        import torch
    except ImportError as e:
        return {"ok": False, "reason": f"torch unavailable: {e}"}
    try:
        folder = checkpoint_dir(config_dir, scenario, node_idx, round_)
        if folder is None:
            return {"ok": False, "reason": "unresolvable checkpoint dir"}
        os.makedirs(folder, exist_ok=True)
        plain = {str(k): (v.detach().cpu() if hasattr(v, "detach") else v)
                 for k, v in dict(state_dict).items()}
        wpath = os.path.join(folder, WEIGHTS_FILE)
        torch.save(plain, wpath)
        has_optim = False
        if optimizer_state is not None:
            try:
                torch.save(optimizer_state, os.path.join(folder, OPTIM_FILE))
                has_optim = True
            except Exception as e:
                logging.warning(f"checkpoint | optimizer not saved: {e}")
        shapes = {}
        try:
            shapes = {k: list(v.shape) if hasattr(v, "shape") else None for k, v in plain.items()}
        except Exception:
            pass
        meta = {
            "scenario": scenario, "node_idx": str(node_idx), "round": int(round_),
            "model_class": (model_info or {}).get("model_class"),
            "model_name": (model_info or {}).get("model_name"),
            "dataset": (model_info or {}).get("dataset"),
            "params": (model_info or {}).get("params"),
            "shapes": shapes,
            "sha256": _sha256_file(wpath),
            "has_optimizer": has_optim,
            "config_snapshot": (model_info or {}).get("config_snapshot") or {},
            "metrics": metrics or {},
            "created_at": time.time(),
        }
        with open(os.path.join(folder, META_FILE), "w") as f:
            json.dump(meta, f, indent=2)
        _prune_old(checkpoint_dir(config_dir, scenario, node_idx, 0), keep_last)
        return {"ok": True, "dir": folder, "round": int(round_), "has_optimizer": has_optim}
    except Exception as e:
        return {"ok": False, "reason": f"save failed: {e}"}


def _prune_old(node_dir: str | None, keep_last: int) -> None:
    """Keep only the newest ``keep_last`` r<N> dirs for a node. Never raises."""
    try:
        if not node_dir or not os.path.isdir(os.path.dirname(node_dir)):
            return
        parent = os.path.dirname(node_dir)
        rounds = []
        for name in os.listdir(parent):
            if name.startswith("r") and name[1:].isdigit():
                rounds.append(int(name[1:]))
        for r in sorted(rounds)[: max(0, len(rounds) - max(1, keep_last))]:
            import shutil

            shutil.rmtree(os.path.join(parent, f"r{r}"), ignore_errors=True)
    except Exception:
        pass


def list_checkpoints(config_dir: str | None, scenario: str | None, node_idx: Any) -> list[int]:
    """Saved rounds for a node, ascending. Never raises."""
    try:
        root = checkpoints_root(config_dir, scenario)
        parent = os.path.join(root, f"participant_{node_idx}")
        out = []
        for name in os.listdir(parent):
            if name.startswith("r") and name[1:].isdigit():
                out.append(int(name[1:]))
        return sorted(out)
    except Exception:
        return []


def read_meta(config_dir: str | None, scenario: str | None, node_idx: Any, round_: Any) -> dict | None:
    """Checkpoint meta.json (None when missing/unreadable). Never raises."""
    try:
        folder = checkpoint_dir(config_dir, scenario, node_idx, round_)
        with open(os.path.join(folder, META_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def validate(config_dir: str | None, scenario: str | None, node_idx: Any, round_: Any) -> tuple[bool, str]:
    """Integrity check: files exist, meta parses, sha256 matches, shapes sane.

    Returns (ok, reason). Never raises. Corrupt/incompatible checkpoints fail
    here with an explicit reason (acceptance) instead of booting silently.
    """
    try:
        folder = checkpoint_dir(config_dir, scenario, node_idx, round_)
        if folder is None or not os.path.isdir(folder):
            return False, f"checkpoint r{round_} of node {node_idx} not found"
        wpath = os.path.join(folder, WEIGHTS_FILE)
        if not os.path.isfile(wpath):
            return False, f"{WEIGHTS_FILE} missing in r{round_}"
        meta = read_meta(config_dir, scenario, node_idx, round_)
        if meta is None:
            return False, f"{META_FILE} missing or unreadable in r{round_}"
        if not isinstance(meta.get("shapes"), dict) or not meta["shapes"]:
            return False, f"{META_FILE} has no tensor shapes in r{round_}"
        digest = _sha256_file(wpath)
        if not meta.get("sha256") or digest != meta["sha256"]:
            return False, (f"sha256 mismatch in r{round_} (file {digest}, "
                           f"meta {meta.get('sha256')}): checkpoint corrupt")
        return True, f"checkpoint r{round_} valid ({len(meta['shapes'])} tensors)"
    except Exception as e:
        return False, f"validation failed: {e}"


def load_weights(
    config_dir: str | None,
    scenario: str | None,
    node_idx: Any,
    round_: Any,
    current_shapes: dict[str, list] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Load weights.pt, optionally filtered to matching shapes.

    ``current_shapes`` enables partial loads (Sprint 6 matrix): reusable keys
    keep their tensors, the rest are reported skipped. Raises ValueError with
    an explicit message when invalid (strict mismatch, corrupt file) — callers
    must not start training in that case. ``map_location="cpu"`` always.
    """
    ok, reason = validate(config_dir, scenario, node_idx, round_)
    if not ok:
        raise ValueError(reason)
    try:
        import torch
    except ImportError as e:
        raise ValueError(f"torch unavailable: {e}")
    folder = checkpoint_dir(config_dir, scenario, node_idx, round_)
    try:
        saved = torch.load(os.path.join(folder, WEIGHTS_FILE), map_location="cpu")
    except Exception as e:
        raise ValueError(f"checkpoint r{round_} unreadable (corrupt): {e}")
    if not isinstance(saved, dict) or not saved:
        raise ValueError(f"checkpoint r{round_} has no tensors")
    if current_shapes is None:
        return {"state": saved, "reusable": sorted(saved), "skipped": []}
    reusable, skipped, filtered = [], [], {}
    try:
        current_shapes = {str(k): list(v) for k, v in dict(current_shapes).items()}
    except Exception:
        raise ValueError(f"checkpoint r{round_}: current shapes unreadable")
    for key, tensor in saved.items():
        try:
            shape = list(tensor.shape)
        except Exception:
            skipped.append(str(key))
            continue
        if str(key) in current_shapes and current_shapes[str(key)] == shape:
            reusable.append(str(key))
            filtered[str(key)] = tensor
        else:
            skipped.append(str(key))
    if strict and (skipped or len(reusable) != len(current_shapes)):
        missing = sorted(set(current_shapes) - set(reusable))
        raise ValueError(f"checkpoint r{round_} incompatible (strict): "
                         f"{len(reusable)} reusable, skipped {skipped}, missing {missing}")
    return {"state": filtered, "reusable": sorted(reusable), "skipped": sorted(skipped)}


def apply_resume(engine: Any, config: Any) -> dict[str, Any]:
    """Node-startup resume (Sprint 6): load parent weights when the participant
    config carries ``scenario_args.resume_from = {scenario, checkpoint_round}``.

    Validates first; corrupt/incompatible checkpoints raise ValueError so the
    node never starts silently on bad state. Warm start: weights load (strict
    for same model, partial otherwise), optimizer state is NOT restored
    pre-fit (no optimizer exists yet — saved for inspection, explicit in meta);
    the federation round counter restarts at 0 while lineage preserves the
    global offset for continuous series. Returns resume info dict.
    """
    try:
        participant = config.participant if not isinstance(config, dict) else config.get("participant", {})
        resume = (participant.get("scenario_args") or {}).get("resume_from")
        if not resume:
            return {"resumed": False}
        parent = resume.get("scenario")
        ckpt_round = int(resume.get("checkpoint_round", 0))
        dataset = (participant.get("data_args") or {}).get("dataset")
        model_name = (participant.get("model_args") or {}).get("model")
        idx = (participant.get("device_args") or {}).get("idx", 0)
        tracking = participant.get("tracking_args") or {}
        import os as _os

        config_dir = _os.environ.get("NEBULA_CONFIG_DIR") or tracking.get("config_dir", "")
        ok, reason = validate(config_dir, parent, idx, ckpt_round)
        if not ok:
            raise ValueError(f"resume refused: {reason}")
        trainer = engine.trainer
        try:
            current_shapes = {k: list(v.shape) for k, v in trainer.model.state_dict().items()}
        except Exception:
            current_shapes = None
        meta = read_meta(config_dir, parent, idx, ckpt_round) or {}
        same_model = str(meta.get("model_name", "")).upper() == str(model_name).upper()
        loaded = load_weights(config_dir, parent, idx, ckpt_round,
                              current_shapes=current_shapes, strict=same_model)
        try:
            trainer.set_model_parameters(loaded["state"], initialize=True)
        except TypeError:
            trainer.model.load_state_dict(loaded["state"], strict=same_model)
        info = {"resumed": True, "parent": parent, "checkpoint_round": ckpt_round,
                "mode": "full" if same_model else "partial",
                "reusable_layers": loaded["reusable"], "skipped_layers": loaded["skipped"],
                "optimizer_restored": False,
                "note": "weights warm-started; fresh optimizer; round counter restarts, "
                        "lineage keeps the global offset"}
        try:
            engine.resume_info = info
        except Exception:
            pass
        logging.info(f"checkpoint | resumed node {idx} from {parent} r{ckpt_round} "
                     f"({info['mode']}, {len(loaded['reusable'])} layers)")
        return info
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"resume failed: {e}")
