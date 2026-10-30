"""Auditable trace store for crew decisions (``crew_trace.jsonl``)."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from typing import Any

from nebula.agentic.schemas import CrewTrace


class TraceStore:
    def __init__(self, *, log_dir: str | None, scenario: str, max_memory: int = 100):
        self._scenario = scenario
        self._memory: deque[dict[str, Any]] = deque(maxlen=max(1, max_memory))
        self._lock = threading.Lock()
        self._path: str | None = None
        safe_scenario = (
            scenario
            and scenario not in (".", "..")
            and os.path.basename(scenario) == scenario
            and "/" not in scenario
            and "\\" not in scenario
        )
        if log_dir and safe_scenario:
            try:
                directory = os.path.join(log_dir, scenario)
                os.makedirs(directory, exist_ok=True)
                self._path = os.path.join(directory, "crew_trace.jsonl")
            except Exception:
                self._path = None

    @property
    def path(self) -> str | None:
        return self._path

    def record(self, trace: CrewTrace) -> None:
        payload = trace.to_dict()
        with self._lock:
            self._memory.append(payload)
            if not self._path:
                return
            try:
                with open(self._path, "a") as f:
                    f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            except Exception as e:
                logging.warning(f"SMAC crew | cannot persist trace: {e}")

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            memory = list(self._memory)
        if len(memory) >= limit or not self._path:
            return memory[-limit:]
        disk: list[dict[str, Any]] = []
        try:
            with open(self._path) as f:
                lines = f.readlines()[-limit:]
            for line in lines:
                try:
                    item = json.loads(line)
                except Exception as e:
                    logging.debug(f"SMAC crew | skip malformed trace line: {e}")
                    continue
                if isinstance(item, dict):
                    disk.append(item)
        except Exception:
            return memory[-limit:]
        return disk or memory[-limit:]

    def last_for_state(self, state_hash: str) -> dict[str, Any] | None:
        for item in reversed(self.recent(200)):
            if item.get("state_hash") == state_hash:
                return item
        return None


__all__ = ["TraceStore"]
