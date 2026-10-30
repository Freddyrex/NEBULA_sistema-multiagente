"""Deterministic guardrails for crew proposals.

The LLM is never trusted to enforce its own limits. These checks run after the
crew returns and before a proposal is registered; the existing backend guards
(cost model, draft validation, RBAC) still run again on confirmation.
"""

from __future__ import annotations

from nebula.agentic.patterns import get_pattern, is_allowed
from nebula.agentic.schemas import ContextPack, CrewDecision, GuardrailCheck, GuardrailReport

_OPERATOR_ROLES = ("admin", "operator", "user")
_COST_REQUIRED_ACTIONS = ("propose_architecture_change", "propose_new_simulation")


class GuardrailEngine:
    def __init__(self, *, min_clients: int = 2, max_exclusion_ratio: float = 0.4):
        self.min_clients = max(1, int(min_clients))
        self.max_exclusion_ratio = max(0.0, min(1.0, float(max_exclusion_ratio)))

    def validate(
        self,
        decision: CrewDecision,
        ctx: ContextPack,
        *,
        role: str = "user",
    ) -> GuardrailReport:
        checks = [
            self._check_whitelist(decision),
            self._check_params(decision),
            self._check_evidence(decision),
            self._check_cost(decision),
            self._check_min_clients(decision, ctx),
            self._check_fairness(decision, ctx),
            self._check_rbac(role),
        ]
        vetoes = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
        return GuardrailReport(passed=not vetoes, checks=checks, vetoes=vetoes)

    def _check_whitelist(self, decision: CrewDecision) -> GuardrailCheck:
        problems: list[str] = []
        for pattern_id, pattern in decision.patterns.items():
            allowed, reason = is_allowed(pattern_id, pattern.params)
            if not allowed:
                problems.append(reason)
        return GuardrailCheck("whitelist", not problems, "; ".join(problems[:3]))

    def _check_params(self, decision: CrewDecision) -> GuardrailCheck:
        problems: list[str] = []
        for pattern_id, pattern in decision.patterns.items():
            if pattern.value == "hold":
                continue
            spec = get_pattern(pattern_id)
            if spec is None:
                continue
            params = pattern.params or {}
            if spec.action_kind == "pause_node" and not str(params.get("node") or "").strip():
                problems.append(f"{pattern_id}: falta node")
            elif spec.action_kind == "select_aggregator":
                algorithm = params.get("algorithm") or params.get("value")
                if not str(algorithm or "").strip():
                    problems.append(f"{pattern_id}: falta algorithm")
            elif spec.action_kind in _COST_REQUIRED_ACTIONS:
                has_change = isinstance(params.get("change"), dict) and bool(params.get("change"))
                has_flat = bool(params.get("kind")) or bool(params.get("changes"))
                if not has_change and not has_flat:
                    problems.append(f"{pattern_id}: cambio sin parámetros")
        return GuardrailCheck("params", not problems, "; ".join(problems[:3]))

    def _check_evidence(self, decision: CrewDecision) -> GuardrailCheck:
        non_hold = {k: v for k, v in decision.patterns.items() if v.value != "hold"}
        if not non_hold:
            return GuardrailCheck("evidence", True, "hold sin cambio")
        if not decision.evidence and not any(v.rationale for v in non_hold.values()):
            return GuardrailCheck("evidence", False, "propuesta sin evidencia ni rationale")
        if not decision.evidence:
            return GuardrailCheck("evidence", False, "propuesta sin evidencia trazable")
        return GuardrailCheck("evidence", True, f"{len(decision.evidence)} evidencias")

    def _check_cost(self, decision: CrewDecision) -> GuardrailCheck:
        problems: list[str] = []
        for pattern_id, pattern in decision.patterns.items():
            if pattern.value == "hold":
                continue
            spec = get_pattern(pattern_id)
            if spec is None:
                continue
            if spec.action_kind in _COST_REQUIRED_ACTIONS:
                cost = (pattern.params or {}).get("cost")
                if not isinstance(cost, dict) or not cost:
                    problems.append(f"{pattern_id}: coste ausente")
        return GuardrailCheck("cost", not problems, "; ".join(problems[:3]))

    def _check_min_clients(self, decision: CrewDecision, ctx: ContextPack) -> GuardrailCheck:
        nodes = ctx.nodes or []
        total = len(nodes)
        if total == 0:
            return GuardrailCheck("min_clients", False, "sin nodos en el contexto")
        online = [n for n in nodes if n.get("online") and not n.get("paused")]
        excluded: set[str] = set()
        for pattern_id, pattern in decision.patterns.items():
            if pattern.value == "hold":
                continue
            spec = get_pattern(pattern_id)
            if spec is None or spec.id != "client_selection":
                continue
            node = str((pattern.params or {}).get("node") or "").strip()
            if node:
                excluded.add(node)
        remaining = [n for n in online if str(n.get("id")) not in excluded]
        if len(remaining) < self.min_clients:
            return GuardrailCheck(
                "min_clients",
                False,
                f"quedarían {len(remaining)} clientes activos < {self.min_clients}",
            )
        if total and len(excluded) / total > self.max_exclusion_ratio:
            return GuardrailCheck(
                "min_clients",
                False,
                f"exclusión {len(excluded)}/{total} > {int(self.max_exclusion_ratio * 100)}%",
            )
        return GuardrailCheck("min_clients", True, f"{len(remaining)} clientes activos")

    def _check_fairness(self, decision: CrewDecision, ctx: ContextPack) -> GuardrailCheck:
        repeated: list[str] = []
        for pattern_id, pattern in decision.patterns.items():
            if getattr(get_pattern(pattern_id), "id", None) != "client_selection":
                continue
            node = str((pattern.params or {}).get("node") or "").strip()
            if not node:
                continue
            hits = 0
            for record in (ctx.decisions or [])[-8:]:
                if not isinstance(record, dict):
                    continue
                kind = str(record.get("kind") or record.get("event") or "")
                if kind not in ("exclude_node", "pause_node"):
                    continue
                if str(record.get("node") or (record.get("params") or {}).get("node") or "") == node:
                    hits += 1
            if hits >= 2:
                repeated.append(f"{node} ya fue excluido {hits} veces (rotación requerida)")
        return GuardrailCheck("fairness", not repeated, "; ".join(repeated[:3]))

    def _check_rbac(self, role: str) -> GuardrailCheck:
        normalized = str(role or "").lower()
        ok = normalized in _OPERATOR_ROLES
        return GuardrailCheck("rbac", ok, "ok" if ok else f"rol sin permiso: {role}")


__all__ = ["GuardrailEngine"]
