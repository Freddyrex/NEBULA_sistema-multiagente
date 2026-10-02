# Applied Improvements — Cognitive Multi-Agent System (SMAC) on NEBULA

**Date:** September 28, 2026
**Design document:** `plan-mas-cognitivo-nebula.md`
**Goal:** formalize NEBULA's `situationalawareness` module as a true multi-agent system
(with persistent beliefs, inter-node coordination and learning) and add an optional cognitive
layer based on an LLM served by Groq — without breaking compatibility with existing scenarios
or configurations.

---

## 1. Executive summary

| Plan phase | Status | What was done |
|---|---|---|
| Phase 1 — MAS framework formalization | ✅ Implemented | `CognitiveAgent`, `SACommand` extension, new arbitration policy, tolerant `SAReasoner` + cognitive hook |
| Phase 2 — Deterministic predictive agents | ✅ Implemented | `StragglerPredictor`, `AggregationSelector`, `ResourceBroker` |
| Phase 3 — Learned policies (bandits) | ⏳ Future | Architecture ready (`CognitiveAgent` + beliefs); predictor uses EWMA + statistical bands |
| Phase 4 — Groq integration | ✅ Implemented | Advisory `GroqReasoningAgent`, structured outputs, timeout, safe degradation |
| Phase 5 — Inter-node coordination | ✅ Implemented | `NegotiationMessage` (protobuf), `NegotiationManager`, opt-in mutual consent in `SANetwork` |
| Phase 6 — Observability and evaluation | ✅ Partial | `CognitiveDecisionLog` (memory + downloadable JSONL); comparative metrics still pending |

**Safety principle applied throughout:** the cognitive/LLM layer is advisory and periodic —
never on the round's critical path. On any failure or timeout, the system keeps running on
the deterministic agents alone.

---

## 2. Phase 1 — MAS framework formalization

### 2.1 `CognitiveAgent` (new)

**Files:**

- `nebula/core/situationalawareness/awareness/cognitive/__init__.py` (new)
- `nebula/core/situationalawareness/awareness/cognitive/cognitiveagent.py` (new)

Formalizes the `SAMComponent` contract as an agent with an explicit
**perception → belief → decision** cycle:

- `perceive()` — local observations without side effects.
- `update_belief() / get_belief() / get_beliefs() / merge_observations()` — persistent
  belief state across rounds (async-lock protected).
- `snapshot()` — anonymized agent summary for the cognitive layer (aggregated metadata
  only; never training data, gradients or weights).
- `sa_component_actions()` — cycle entry point, implemented by each subclass.

### 2.2 `SACommand` extension (modified)

**File:** `nebula/core/situationalawareness/awareness/sautils/sacommand.py`

- New `SACommand` fields: `confidence` (float in [0, 1], `1.0` for deterministic agents),
  `explanation` (auditable text) and `ttl_rounds` (round-based expiry).
- New methods: `get_confidence()`, `get_explanation()`, `get_ttl_rounds()`,
  `set_issued_round()`, `is_expired(current_round)`, `get_command_type()`, `to_dict()`
  (serialization for prompts, logs and dashboard) and `from_llm_decision()` (maps the
  structured LLM JSON to an `SACommand`; `no_action` → `None`).
- New types `SACommandType.TRAINING` and `SACommandType.COGNITIVE`.
- New actions: `ADJUST_TRAINING_POLICY`, `REQUEST_TOPOLOGY_CHANGE`, `SELECT_AGGREGATOR`,
  `FLAG_FOR_REVIEW`, `PAUSE_LOCAL_TRAINING`, `REDUCE_BATCH_SIZE`, `EXCLUDE_NODE`.
- New `TrainingCommand` and `CognitiveCommand` subclasses (the latter defaults to `LOW`
  priority and a 3-round TTL: advisory, not authoritative), registered in
  `factory_sa_command` as `"training"` and `"cognitive"`.
- `ConnectivityCommand` and `AggregationCommand` accept `confidence/explanation/ttl` as
  keyword-only arguments (no existing positional call broken).

### 2.3 Arbitration policy with cognitive tie-break (new + modified)

**Files:**

- `nebula/core/situationalawareness/awareness/arbitrationpolicies/priorityllmtiebreak.py` (new)
- `nebula/core/situationalawareness/awareness/arbitrationpolicies/arbitrationpolicy.py` (modified)
- `nebula/core/situationalawareness/awareness/arbitrationpolicies/staticarbitrationpolicy.py` (modified)

- New `PriorityLLMTiebreakPolicy`, selectable as `priority_with_llm_tiebreak` (also added
  to the frontend dropdown, see §6). Inherits the static rule and adds `is_ambiguous_tie()`:
  detects unresolvable ties (same priority **and** same static weight) to request an LLM
  "second opinion".
- Static weights extended without touching existing ones (`SATraining: 1`, `SANetwork: 2`,
  `SAReputation: 3`): `StragglerPredictor: 2`, `AggregationSelector: 3`,
  `ResourceBroker: 1` and `GroqReasoning: 0` (the LLM advises: under the static fallback it
  always loses to direct telemetry).

### 2.4 Hardened `SAReasoner` (modified)

**File:** `nebula/core/situationalawareness/awareness/sareasoner.py`

- **Tolerant component loading:** missing config sections log a warning and are skipped
  (compatible with pre-SMAC scenarios); explicit `class_name` support for multi-word agents.
- **Round tracking** (`current_round`) from `RoundEndEvent`.
- **Cognitive tie-break hook** (`_resolve_tie_with_cognition`): on ambiguous ties it asks
  the `GroqReasoningAgent` under a bounded timeout; on failure or abstention it applies the
  static rule. The round never blocks on the LLM.
- **Expiry filtering** (TTL) before arbitration.
- **Decision recording**: every selected command goes to the `CognitiveDecisionLog`
  (see §7) with its issue round stamped.
- Exposes `get_component()`, `current_round`, `decision_log` and `negotiation`.
- `stop()` now also stops the `NegotiationManager`.

---

## 3. Phase 2 — Deterministic predictive agents

### 3.1 `StragglerPredictor` (new)

**Files:** `nebula/core/situationalawareness/awareness/stragglerpredictor/__init__.py`,
`stragglerpredictor.py` (+ `StragglerPredictorAgent` as the `SAModuleAgent` wrapper).

- Observes `RoundStartEvent` (round start), `UpdateReceivedEvent` (per-neighbor arrival
  latency) and `RoundEndEvent` (round-duration baseline).
- Keeps configurable sliding windows (`history_window: 20`) and predicts with
  `mean + std_factor·σ > deadline` (`deadline = max(median·2, 10s)`).
- Emits `EXCLUDE_NODE` commands (`HIGH` priority, 2-round TTL) with confidence proportional
  to the deadline overrun. Pure, testable `predict_stragglers()` function.
- Conservative without enough baseline or samples (`min_samples: 3`): no prediction.

### 3.2 `AggregationSelector` (new)

**Files:** `nebula/core/situationalawareness/awareness/aggregationselector/__init__.py`,
`aggregationselector.py` (+ `AggregationSelectorAgent`).

- Reacts to `AggregationEvent`, evaluating received vs. missing updates, blacklisted nodes
  and update-weight dispersion.
- Deterministic rules (`choose_aggregation()`): Byzantine suspicion with ≥4 updates →
  `Krum` (`Median` fallback with fewer); many missing updates or high dispersion →
  `TrimmedMean`; moderate dispersion → `Median`; stable context → `FedAvg`.
- The `SELECT_AGGREGATOR` command **applies the selection for real** (aggregator hot-swap
  via `create_aggregator` + `init()`, updating `aggregator_args.algorithm`) and **returns
  the updates untouched**, so it is safe in any arbitration position. Without engine access
  (tests), the choice is kept as an advisory belief.
- Only emits a command when the choice changes (keeps arbitration noise-free).

### 3.3 `ResourceBroker` (new)

**Files:** `nebula/core/situationalawareness/awareness/resourcebroker/__init__.py`,
`resourcebroker.py` (+ `ResourceBrokerAgent`).

- Polls `SystemMonitor` on every round end with **fast probes only** (the 5 s network probe
  is deliberately skipped to never delay arbitration) + GPU via NVML when available.
- Critical pressure (≥95% CPU/RAM/GPU) → `PAUSE_LOCAL_TRAINING` (`HIGH`); high usage
  (≥85% CPU, ≥90% RAM/GPU, configurable) → `REDUCE_BATCH_SIZE` (`MEDIUM`, 0.5 factor).
- Round-based cooldown (`cooldown_rounds: 3`) against flapping. Pure, testable
  `decide_resource_action()` function.

---

## 4. Phase 4 — Cognitive layer with Groq

**Files:** `nebula/core/situationalawareness/awareness/cognitive/groqreasoningagent.py`,
`nebula/core/situationalawareness/awareness/groqreasoningagent/__init__.py`,
`nebula/core/situationalawareness/awareness/groqreasoningagent/groqreasoningagent.py`
(loader shim for the dynamic component loader).

- `GroqReasoning(CognitiveAgent)` + `GroqReasoningAgent` wrapper (`GroqReasoning_MainAgent`,
  arbitration weight 0).
- `httpx.AsyncClient` calls to `POST https://api.groq.com/openai/v1/chat/completions` with
  strict-JSON-schema `response_format` (`DECISION_SCHEMA`: `decision`, `target_agent`,
  `parameters`, `confidence`, `explanation`) and in-code response validation.
- Default model `openai/gpt-oss-20b` with `reasoning_effort=low` (configurable; the model
  name is **never** hardcoded).
- Two invocation modes: **periodic** (every `invoke_every_n_rounds: 5` rounds) and
  **on-demand** as tie-breaker (`resolve_tie()` maps `parameters.preferred_action` to the
  winning command; abstention → static fallback).
- **Privacy:** only aggregated, anonymized metadata (node addresses hashed with truncated
  SHA-256, `anonymize_addr`). Never weights, gradients or data.
- **Safe degradation:** aggressive timeout (0.3 s); any transport/parsing/validation error
  → no suggestion; without an API key the agent self-disables with a warning. The key is
  read from `GROQ_API_KEY` (configurable via `api_key_env_var`), **never** from scenario JSON.
- `httpx==0.28.1` added to the `core` group of `pyproject.toml` + `uv.lock` (minimal 4-line
  diff; it was already present as a transitive dependency).
- **Official Groq SDK** (`groq==1.7.0` in the `core` and `frontend` groups): every call goes
  through `nebula/core/smac_groq.py` (`AsyncGroq`, `temperature=1`, `top_p=1`,
  `max_completion_tokens=2048`, configurable `reasoning_effort`, `stream=False`, structured
  outputs), with transparent fallback to `httpx` on images without the SDK. The chat
  defaults to `openai/gpt-oss-120b` + `medium` effort (`SMAC_CHAT_MODEL` /
  `SMAC_CHAT_REASONING_EFFORT` envs); node agents keep `gpt-oss-20b` + `low` for latency
  (changeable per scenario).

---

## 5. Phase 5 — Inter-node coordination (contract-net)

### 5.1 New protobuf message (modified + regenerated)

**Files:** `nebula/core/pb/nebula.proto`, `nebula/core/pb/nebula_pb2.py` (regenerated),
`nebula/core/network/actions.py`, `nebula/core/network/messages.py`.

- `NegotiationMessage { PROPOSE | ACCEPT | REJECT | COUNTER_PROPOSAL, proposal_id, edge_a,
  edge_b, change (connect|disconnect), reason, ttl_rounds, round }` + field 16 in the
  `Wrapper` oneof.
- `nebula_pb2.py` regenerated with `grpcio-tools==1.60.0` (protobuf-4-era protoc,
  compatible with the project's `protobuf==4.25.3` runtime; round-trip verified).
- `NegotiationAction` registered in `ACTION_CLASSES` and a `"negotiation"` template in
  `MessagesManager` (creation/parsing verified).

### 5.2 `NegotiationManager` (new) + mutual consent

**Files:** `nebula/core/situationalawareness/awareness/negotiation/__init__.py`,
`negotiationmanager.py`; modified `sanetwork.py` (methods + `DISCONNECT` branch).

- Protocol: A proposes an edge change to B → B evaluates with its local `SAReasoner`
  (no restructure in progress + no conflicting pending proposal) and replies
  `ACCEPT/REJECT` (an incoming `COUNTER_PROPOSAL` is evaluated as a proposal under the
  same id).
- UUID proposals, round-based TTL (`timeout_rounds: 2`, expiry on `RoundEndEvent`) and
  bounded waits (`wait_timeout_s: 5.0`).
- `SANetwork` proposes when suggesting `DISCONNECT` and wraps execution:
  **it only disconnects on mutual ACCEPT**; on REJECT/timeout it skips (the proposal
  expires and may be retried). Parallelizable command so it never blocks the rest.
- **Opt-in and safe:** `sa_reasoner.negotiation.enabled` defaults to `false`, so mixed
  federations (peers without SMAC that would never answer) keep the classic unilateral
  behavior.

---

## 6. Configuration and frontend (modified)

**Files:** `nebula/controller/scenarios.py`,
`nebula/frontend/templates/deployment.html`.

- `scenarios.py` generates the new sections with the plan's defaults: `straggler_predictor`
  (`history_window: 20`, `min_samples: 3`, `std_factor: 2.0`), `aggregation_selector`
  (`missing_ratio_threshold: 0.3`, `variance_threshold: 1.0`), `resource_broker`
  (`cpu_high: 85.0`, `mem_high: 90.0`, `gpu_mem_high: 90.0`, `cooldown_rounds: 3`),
  `groq_reasoning_agent` (`model: openai/gpt-oss-20b`, `reasoning_effort: low`,
  `timeout_s: 0.3`, `invoke_every_n_rounds: 5`, `invoke_on_tiebreak: true`) and
  `negotiation` (disabled by default).
- The dashboard *Arbitration Policy* dropdown now offers `priority_with_llm_tiebreak`
  alongside `sap`.

---

## 7. Observability (Phase 6, partial)

**File:** `nebula/core/situationalawareness/awareness/cognitive/decisionlog.py`

- `CognitiveDecisionLog`: in-memory ring buffer (500 records) + JSONL persistence at
  `app/logs/<scenario>/smac_decisions.jsonl` (timestamp, round, owner, action, type,
  priority, confidence, explanation). Living inside the scenario log folder, it is
  automatically included in the dashboard *"Download logs"* zip.
- Comparative evaluation metrics (plan §8 table) still pending runs on scenarios with
  injected churn/mobility/stragglers.

---

## 8. Compatibility and deployment

- **Backward compatible:** older scenarios and JSONs (without SMAC sections) load as
  before; new components are skipped with a warning. No existing signature changed
  incompatibly (keyword-only additions with defaults).
- **No image rebuilds:** frontend, controller and nodes bind-mount the repo live
  (`<root>:/nebula`), so restarting the platform
  (`python app/main.py --stop` + `python app/main.py`) picks up the code. Old scenarios
  must be **recreated** to get the new config sections.
- Existing arbitration weights untouched; the LLM cannot overrule telemetry.

---

## 9. Verification performed

- Scratch suite `/tmp/test_smac.py`: **51/51 checks** (`SACommand` extensions,
  `from_llm_decision` + edge cases, factories and fallback, ambiguous ties, pure logic of
  the 3 agents, decision log in memory and file, Groq with mocked transport —
  success/timeout/no-key/invalid payload/resolve_tie —, negotiation manager and protobuf
  round-trip). It caught and fixed a real bug (unreachable `Median` branch in the selector).
- `ruff check` clean on all new files; `ruff format` applied.
- Imports of all 19 touched modules + dynamic-loader resolution for all 6 components
  (`sa_network`, `sa_training` and the 4 new ones) verified.

---

## 10. Future work

1. **Phase 3:** wrap neighbor/training policies with contextual bandits using current
   heuristics as fallback (foundation ready: beliefs + `snapshot()`).
2. **Full Phase 6:** decisions/explanations panel in the frontend and a comparative
   scenario battery with the plan's metrics.
3. Negotiation follow-ups: active `COUNTER_PROPOSAL` generation (incoming ones are
   evaluated today) and local-LLM consultation on ambiguous proposals.
4. Enable `negotiation.enabled: true` by default once the whole federation runs SMAC.
5. Check `https://console.groq.com/docs/models` before each deployment (the model catalog
   changes often) and adjust `model` in the configuration accordingly.

---

## 11. SMAC Chat conversational chatbot (new)

**New files:** `nebula/frontend/smac_chat.py` (backend),
`nebula/frontend/templates/smac_chat.html`, `nebula/frontend/static/js/smac-chat.js`,
`nebula/core/operatorbus.py`.
**Modified:** `nebula/frontend/app.py` (3 routes), `nebula/frontend/templates/dashboard.html`
(buttons), `nebula/core/engine.py` (minimal hooks), `app/deployer.py` +
`nebula/controller/scenarios.py` (`GROQ_API_KEY` passthrough), `pyproject.toml` +
`uv.lock` (`httpx` in the `frontend` group).

The multi-agent system is operated from the dashboard through the **SMAC cognitive
panel** (*SMAC Chat* button or per-scenario chat icon, with the scenario preselected).
It is not a linear text chat but a **cognitive observability and interactive control
panel** split spatially: status header (round, aggregator, counts), **live SVG topology**
correlated with reasoning (click a node to inspect it and highlight its decisions),
**reasoning feed** (decision cards with confidence and explanation + on-demand LLM
analysis via `/platform/smac/analyze`), **controls** (Pause/Resume, aggregator switch,
disconnect proposal, stop — proposed via `/platform/smac/propose` and only executed after
*Confirm & run*) and a compact natural-language input as a secondary channel. The panel
polls `/platform/smac/state` (scenario + topology + nodes + decisions) every 5 s.

- **Query:** the backend builds a live scenario snapshot (config, node rows, Docker
  container states, latest SMAC decisions with explanations) and the LLM answers grounded
  in it (rounds, stragglers, topology, why-explanations).
- **Act (always confirmed):** the LLM proposes structured actions and the UI shows
  *Confirm & run / Discard*. v1 actions: `pause_node`/`resume_node` (Docker
  pause/unpause: instant freeze without state loss), `select_aggregator` (hot-swap via
  operator bus), `propose_disconnect` (inter-node negotiation via bus), `stop_scenario`
  (controller API).
- **Operator bus:** files under `app/config/<scenario>/operator_commands/` consumed by
  each node via `OperatorBus` (5 s poll, per-node results, TTL, no double execution);
  the engine honors `pause` (an `operator_paused` flag checked every round) and starts/
  stops the bus in `deploy_components`/`_shutdown_protocol`.
- **Safety:** session-required routes, single-use pending actions with a 10 min TTL,
  `GROQ_API_KEY` auto-forwarded into frontend/controller/nodes (without it the chat
  reports itself as disabled).

**Verification:** `/tmp/test_smac_chat.py` suite **27/27** (context, mocked Groq,
pending/confirmation flow, bus round-trip with pause/broadcast/algorithm/disconnect and
graceful executors); clean `ruff check`, `ruff format` applied; earlier SMAC suite still
**51/51**.
