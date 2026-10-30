"""Typed contracts for the NEBULA multi-agent decision layer (SMAC / CrewAI).

The module is intentionally dependency-free (stdlib dataclasses + JSON Schema
dicts): the same contracts are used by the controller/frontend (where pydantic
is available) and by the node cognitive agent, whose ``core`` dependency group
does not install pydantic or CrewAI.

Every model exposes ``to_dict()``/``from_dict()`` plus a validating parser that
returns ``None`` instead of raising, so callers can degrade safely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nebula.agentic.patterns import available_patterns

SCHEMA_VERSION = "v1"

DECISIONS = ("on", "off", "hold", "change")

EVIDENCE_SOURCES = (
    "scenario_config",
    "metrics_digest",
    "metrics_series",
    "findings",
    "cost_model",
    "decision_log",
    "runtime",
    "conversation",
)

_ACTION_KINDS = (
    "pause_node",
    "resume_node",
    "select_aggregator",
    "propose_architecture_change",
    "propose_new_simulation",
    "hold",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(value: Any, limit: int, default: str = "") -> str:
    try:
        text = default if value is None else str(value)
    except Exception:
        text = default
    return text[:limit]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Evidence:
    source: str
    ref: str
    metric: str | None = None
    round: int | None = None
    node: str | None = None
    value: Any = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ref": self.ref,
            "metric": self.metric,
            "round": self.round,
            "node": self.node,
            "value": self.value,
            "detail": _clip(self.detail, 500, "") or None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Evidence | None:
        if not isinstance(raw, dict):
            return None
        source = str(raw.get("source") or "").strip()
        ref = str(raw.get("ref") or "").strip()
        if source not in EVIDENCE_SOURCES or not ref:
            return None
        return cls(
            source=source,
            ref=_clip(ref, 200),
            metric=_clip(raw.get("metric"), 80, "") or None,
            round=max(0, _as_int(raw.get("round")) or 0) if raw.get("round") is not None else None,
            node=_clip(raw.get("node"), 80, "") or None,
            value=raw.get("value"),
            detail=_clip(raw.get("detail"), 500, "") or None,
        )


@dataclass
class AgentVerdict:
    agent: str
    pattern: str
    decision: str
    confidence: float
    rationale: str
    evidence: list[Evidence] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    proposed_params: dict[str, Any] = field(default_factory=dict)
    guardrails: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "pattern": self.pattern,
            "decision": self.decision,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "evidence": [e.to_dict() for e in self.evidence],
            "risks": self.risks[:5],
            "proposed_params": self.proposed_params,
            "guardrails": self.guardrails[:8],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> AgentVerdict | None:
        if not isinstance(raw, dict):
            return None
        agent = _clip(raw.get("agent"), 80).strip()
        pattern = _clip(raw.get("pattern"), 80).strip()
        decision = str(raw.get("decision") or "").strip().lower()
        rationale = _clip(raw.get("rationale"), 1200).strip()
        if not agent or not pattern or decision not in DECISIONS or not rationale:
            return None
        evidence = [e for e in (Evidence.from_dict(x) for x in (raw.get("evidence") or [])) if e]
        if not evidence:
            return None
        params = raw.get("proposed_params")
        risks = raw.get("risks")
        guardrails = raw.get("guardrails")
        return cls(
            agent=agent,
            pattern=pattern,
            decision=decision,
            confidence=max(0.0, min(1.0, _as_float(raw.get("confidence")))),
            rationale=rationale,
            evidence=evidence[:12],
            risks=[_clip(r, 300) for r in (risks if isinstance(risks, list) else [])][:5],
            proposed_params=params if isinstance(params, dict) else {},
            guardrails=[_clip(g, 200) for g in (guardrails if isinstance(guardrails, list) else [])][:8],
        )


@dataclass
class PatternDecision:
    value: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "params": self.params,
            "confidence": self.confidence,
            "rationale": _clip(self.rationale, 800),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> PatternDecision | None:
        if not isinstance(raw, dict):
            return None
        value = str(raw.get("value") or "").strip().lower()
        if value not in DECISIONS:
            return None
        params = raw.get("params")
        return cls(
            value=value,
            params=params if isinstance(params, dict) else {},
            confidence=max(0.0, min(1.0, _as_float(raw.get("confidence")))),
            rationale=_clip(raw.get("rationale"), 800),
        )


@dataclass
class GuardrailCheck:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": bool(self.passed), "detail": _clip(self.detail, 300)}

    @classmethod
    def from_dict(cls, raw: Any) -> GuardrailCheck | None:
        if not isinstance(raw, dict):
            return None
        name = _clip(raw.get("name"), 80).strip()
        if not name:
            return None
        return cls(name=name, passed=bool(raw.get("passed")), detail=_clip(raw.get("detail"), 300))


@dataclass
class GuardrailReport:
    passed: bool
    checks: list[GuardrailCheck] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "checks": [c.to_dict() for c in self.checks],
            "vetoes": [_clip(v, 300) for v in self.vetoes],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> GuardrailReport:
        if not isinstance(raw, dict):
            return cls(passed=False, vetoes=["guardrail report missing"])
        checks = [c for c in (GuardrailCheck.from_dict(x) for x in (raw.get("checks") or [])) if c]
        vetoes = [_clip(v, 300) for v in (raw.get("vetoes") if isinstance(raw.get("vetoes"), list) else [])]
        return cls(passed=bool(raw.get("passed")), checks=checks, vetoes=vetoes)


@dataclass
class CrewDecision:
    patterns: dict[str, PatternDecision]
    global_rationale: str
    confidence: float
    requires_human_review: bool = False
    guardrails: GuardrailReport = field(default_factory=lambda: GuardrailReport(passed=False))
    evidence: list[Evidence] = field(default_factory=list)
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "patterns": {k: v.to_dict() for k, v in self.patterns.items()},
            "global_rationale": _clip(self.global_rationale, 2000),
            "confidence": self.confidence,
            "requires_human_review": bool(self.requires_human_review),
            "guardrails": self.guardrails.to_dict(),
            "evidence": [e.to_dict() for e in self.evidence[:24]],
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CrewDecision | None:
        if not isinstance(raw, dict):
            return None
        patterns_raw = raw.get("patterns")
        if not isinstance(patterns_raw, dict) or not patterns_raw:
            return None
        patterns: dict[str, PatternDecision] = {}
        for key, value in patterns_raw.items():
            parsed = PatternDecision.from_dict(value)
            if parsed is not None:
                patterns[_clip(key, 80)] = parsed
        if not patterns:
            return None
        rationale = _clip(raw.get("global_rationale"), 2000).strip()
        if not rationale:
            return None
        evidence = [e for e in (Evidence.from_dict(x) for x in (raw.get("evidence") or [])) if e]
        return cls(
            patterns=patterns,
            global_rationale=rationale,
            confidence=max(0.0, min(1.0, _as_float(raw.get("confidence")))),
            requires_human_review=bool(raw.get("requires_human_review", False)),
            guardrails=GuardrailReport.from_dict(raw.get("guardrails")),
            evidence=evidence[:24],
            trace_id=_clip(raw.get("trace_id"), 64),
        )


@dataclass
class SafetyVerdict:
    passed: bool
    vetoes: list[str] = field(default_factory=list)
    checks: list[GuardrailCheck] = field(default_factory=list)
    requires_human_review: bool = False
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "vetoes": [_clip(v, 300) for v in self.vetoes],
            "checks": [c.to_dict() for c in self.checks],
            "requires_human_review": bool(self.requires_human_review),
            "rationale": _clip(self.rationale, 1200),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SafetyVerdict | None:
        if not isinstance(raw, dict):
            return None
        checks = [c for c in (GuardrailCheck.from_dict(x) for x in (raw.get("checks") or [])) if c]
        vetoes = [_clip(v, 300) for v in (raw.get("vetoes") if isinstance(raw.get("vetoes"), list) else [])]
        return cls(
            passed=bool(raw.get("passed")),
            vetoes=vetoes,
            checks=checks,
            requires_human_review=bool(raw.get("requires_human_review", False)),
            rationale=_clip(raw.get("rationale"), 1200),
        )


@dataclass
class ContextPack:
    scenario: dict[str, Any]
    round: int | None
    config: dict[str, Any]
    nodes: list[dict[str, Any]]
    metrics: dict[str, Any]
    findings: list[dict[str, Any]]
    guardrails: dict[str, Any]
    provenance: list[dict[str, Any]]
    state_hash: str
    series: dict[str, Any] = field(default_factory=dict)
    costs: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    conversation: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "round": self.round,
            "config": self.config,
            "nodes": self.nodes,
            "metrics": self.metrics,
            "series": self.series,
            "findings": self.findings,
            "costs": self.costs,
            "decisions": self.decisions,
            "conversation": self.conversation,
            "guardrails": self.guardrails,
            "provenance": self.provenance,
            "state_hash": self.state_hash,
        }

    def to_prompt_payload(self, *, max_bytes: int = 6000) -> dict[str, Any]:
        """Compact, redacted payload for an LLM prompt.

        Keeps the fields agents need (config, round, findings, top metrics and
        costs) and drops series/conversation when the JSON exceeds ``max_bytes``.
        Never raises; returns the full dict when it already fits.
        """
        payload = self.to_dict()
        try:
            if len(json.dumps(payload)) <= max_bytes:
                return payload
        except Exception:
            return payload
        compact = {
            "scenario": payload["scenario"],
            "round": payload["round"],
            "config": payload["config"],
            "nodes": payload["nodes"][:32],
            "metrics": payload["metrics"],
            "findings": payload["findings"][:16],
            "costs": payload["costs"],
            "decisions": payload["decisions"][-5:],
            "guardrails": payload["guardrails"],
            "state_hash": payload["state_hash"],
        }
        if len(json.dumps(compact)) <= max_bytes:
            return compact
        compact["nodes"] = compact["nodes"][:12]
        compact["findings"] = compact["findings"][:8]
        compact["metrics"] = {
            k: v for k, v in compact["metrics"].items()
            if k in ("accuracy", "f1_macro", "loss", "round_time_s", "cpu_percent", "ram_percent")
        }
        return compact


@dataclass
class CrewTrace:
    trace_id: str
    scenario: str
    round: int | None
    strategy: str
    state_hash: str
    started_at: str
    finished_at: str | None = None
    latency_ms: int | None = None
    fallback: str | None = None
    agents: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    final_decision: dict[str, Any] | None = None
    guardrails: dict[str, Any] | None = None
    prompt_hash: str | None = None
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    schema_version: str = SCHEMA_VERSION
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "scenario": self.scenario,
            "round": self.round,
            "strategy": self.strategy,
            "state_hash": self.state_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency_ms": self.latency_ms,
            "fallback": self.fallback,
            "agents": self.agents,
            "verdicts": self.verdicts,
            "final_decision": self.final_decision,
            "guardrails": self.guardrails,
            "prompt_hash": self.prompt_hash,
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "schema_version": self.schema_version,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CrewTrace | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                trace_id=str(raw["trace_id"]),
                scenario=str(raw["scenario"]),
                round=_as_int(raw.get("round")),
                strategy=str(raw["strategy"]),
                state_hash=str(raw["state_hash"]),
                started_at=str(raw.get("started_at") or utc_now()),
                finished_at=raw.get("finished_at"),
                latency_ms=_as_int(raw.get("latency_ms")),
                fallback=raw.get("fallback"),
                agents=list(raw.get("agents") or []),
                verdicts=list(raw.get("verdicts") or []),
                final_decision=raw.get("final_decision"),
                guardrails=raw.get("guardrails"),
                prompt_hash=raw.get("prompt_hash"),
                llm_calls=_as_int(raw.get("llm_calls")) or 0,
                tokens_in=_as_int(raw.get("tokens_in")) or 0,
                tokens_out=_as_int(raw.get("tokens_out")) or 0,
                schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
                error=raw.get("error"),
            )
        except Exception:
            return None


_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": list(EVIDENCE_SOURCES)},
        "ref": {"type": "string"},
        "metric": {"type": "string"},
        "round": {"type": "integer"},
        "node": {"type": "string"},
        "value": {"type": "string"},
        "detail": {"type": "string"},
    },
    "required": ["source", "ref"],
}

_PATTERN_IDS = tuple(sorted(available_patterns()))

_PATTERN_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "enum": list(DECISIONS)},
        "params": {"type": "object"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["value", "confidence", "rationale"],
}

_PROPOSED_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "node": {"type": "string"},
        "algorithm": {
            "type": "string",
            "enum": ["fedavg", "krum", "median", "trimmedmean", "FedAvg", "Krum", "Median", "TrimmedMean"],
        },
        "kind": {"type": "string", "enum": ["rounds", "epochs", "aggregator", "model", "topology", "batch"]},
        "total_rounds": {"type": "integer"},
        "local_epochs": {"type": "integer"},
        "batch_size": {"type": "integer"},
        "model": {"type": "string"},
        "topology": {"type": "string"},
        "value": {"type": "string"},
        "change": {"type": "object"},
        "changes": {"type": "object"},
        "cost": {"type": "object"},
        "federation": {"type": "string"},
    },
}

AGENT_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {"type": "string"},
        "pattern": {"type": "string", "enum": list(_PATTERN_IDS)},
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "risks": {"type": "array", "items": {"type": "string"}},
        "proposed_params": _PROPOSED_PARAMS_SCHEMA,
        "guardrails": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["agent", "pattern", "decision", "confidence", "rationale", "evidence"],
}

SAFETY_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "vetoes": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
                "required": ["name", "passed"],
            },
        },
        "requires_human_review": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["passed", "checks"],
}

CREW_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "object",
            "properties": dict.fromkeys(_PATTERN_IDS, _PATTERN_DECISION_SCHEMA),
        },
        "global_rationale": {"type": "string"},
        "confidence": {"type": "number"},
        "requires_human_review": {"type": "boolean"},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
    },
    "required": ["patterns", "global_rationale", "confidence"],
}


def parse_agent_verdict(raw: Any) -> AgentVerdict | None:
    return AgentVerdict.from_dict(raw)


def parse_safety_verdict(raw: Any) -> SafetyVerdict | None:
    return SafetyVerdict.from_dict(raw)


def parse_decision(raw: Any) -> CrewDecision | None:
    return CrewDecision.from_dict(raw)


def parse_trace(raw: Any) -> CrewTrace | None:
    return CrewTrace.from_dict(raw)


__all__ = [
    "AGENT_VERDICT_SCHEMA",
    "CREW_DECISION_SCHEMA",
    "DECISIONS",
    "EVIDENCE_SOURCES",
    "SAFETY_VERDICT_SCHEMA",
    "SCHEMA_VERSION",
    "AgentVerdict",
    "ContextPack",
    "CrewDecision",
    "CrewTrace",
    "Evidence",
    "GuardrailCheck",
    "GuardrailReport",
    "PatternDecision",
    "SafetyVerdict",
    "parse_agent_verdict",
    "parse_decision",
    "parse_safety_verdict",
    "parse_trace",
    "utc_now",
]
