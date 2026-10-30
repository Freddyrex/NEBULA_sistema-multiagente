from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from datetime import UTC, datetime


class CognitiveDecisionLog:
    """Auditable log of the commands selected by SA arbitration (SMAC).

    Every command that survives arbitration is recorded with its owner,
    action, priority, confidence, explanation and round. Records are kept
    in a bounded in-memory ring buffer and appended to a JSONL file inside
    the scenario log directory, so they are included automatically in the
    "download logs" zip served by the frontend.
    """

    def __init__(self, maxlen: int = 500, log_dir: str | None = None, scenario_name: str | None = None):
        self._records: deque[dict] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        self._log_dir = log_dir
        self._scenario_name = scenario_name
        self._file_path: str | None = None
        if log_dir and scenario_name:
            try:
                folder = os.path.join(log_dir, scenario_name)
                os.makedirs(folder, exist_ok=True)
                self._file_path = os.path.join(folder, "smac_decisions.jsonl")
            except Exception as e:
                logging.warning(f"CognitiveDecisionLog | cannot create log file: {e}")

    async def record(
        self,
        owner: str,
        action: str,
        command_type: str,
        priority: int,
        confidence: float,
        explanation: str,
        round_number: int | None = None,
        extra: dict | None = None,
    ):
        """Append a decision record (memory + JSONL file)."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "round": round_number,
            "owner": owner,
            "action": action,
            "command_type": command_type,
            "priority": priority,
            "confidence": confidence,
            "explanation": explanation,
            "extra": extra or {},
        }
        async with self._lock:
            self._records.append(entry)
        if self._file_path:
            try:
                with open(self._file_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logging.warning(f"CognitiveDecisionLog | cannot persist record: {e}")

    async def recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent records (newest last)."""
        async with self._lock:
            return list(self._records)[-limit:]
