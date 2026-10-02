# Cognitive Multi-Agent System (SMAC)

NEBULA ships a **Situational Awareness (SA)** module that works as a *per-node,
single-agent* framework: each node runs an `SAReasoner` that loads components
(`SAMComponent`), collects their suggestions on a blackboard (`SuggestionBuffer`) and
arbitrates them by priority. The **Cognitive Multi-Agent System (SMAC)** formalizes this
into a true multi-agent system and adds an optional **cognitive layer** powered by an LLM
served through [Groq](https://console.groq.com/docs/models).

Design principles:

- The cognitive layer is **advisory and periodic** — never on the training round's
  critical path. If the LLM is slow or unavailable, the node keeps working with the
  deterministic agents only (**safe degradation**).
- Every new agent speaks the same contract (`SAMComponent` / `SACommand`), so no parallel
  framework is introduced.
- Privacy: only aggregated, anonymized metadata ever leaves the node. Training data,
  gradients and model weights are never sent anywhere.

## Architecture

```
CAPA COGNITIVA (opcional) .......... GroqReasoningAgent -> LLM on Groq
  advises on ambiguous ties, explains decisions      (async, timeout, non-blocking)
CAPA DE AGENTES .................... SAReasoner: SANetwork, SATraining,
  StragglerPredictor, AggregationSelector, ResourceBroker (+ arbitration)
CAPA DE PERCEPCION ................. SystemMonitor, RoundEndEvent, AggregationEvent, ...
COORDINACION INTER-NODO ............ contract-net negotiation over CommunicationsManager
```

## Agents

| Agent | Trigger | Input | Output (`SACommand`) |
|---|---|---|---|
| `SANetwork` | `UpdateNeighborEvent` / `NodeFoundEvent` | known neighbors, target topology | restructure connection |
| `SATraining` | `RoundEndEvent` | round metrics, neighbors | tune local epochs / policy |
| `StragglerPredictor` | `RoundEndEvent` (+ latency observations) | per-neighbor latency history | `EXCLUDE_NODE` for predicted stragglers |
| `AggregationSelector` | `AggregationEvent` | received/missing updates, blacklist, weight dispersion | `SELECT_AGGREGATOR` (`FedAvg`/`Krum`/`Median`/`TrimmedMean`, hot-swapped) |
| `ResourceBroker` | `RoundEndEvent` | CPU/GPU/RAM pressure | `PAUSE_LOCAL_TRAINING` / `REDUCE_BATCH_SIZE` |
| `GroqReasoningAgent` | every N rounds or arbitration tie | aggregated snapshot of sibling agents | natural-language explanation + high-level suggestion |

The arbitration policy `priority_with_llm_tiebreak` behaves like the static policy except
on **ambiguous ties** (same priority and same static weight), where the LLM is asked for a
second opinion under a short timeout. The static rule always applies as fallback.

## Enabling the cognitive layer (Groq)

1. Create scenarios with Situational Awareness enabled (as usual). New scenarios already
   include the SMAC sections with safe defaults.
2. Export your key **before starting the platform** (it is never stored in scenario JSON).
   Alternatively, write it (first line) into a file named `apikey` at the repository root
   (`chmod 600 apikey`; gitignored): the deployer loads it automatically and forwards it
   into the frontend, controller and node containers.

```bash
export GROQ_API_KEY="gsk_..."
python app/main.py
```

3. (Optional) In the deployment form, set *Arbitration Policy* to
   `priority_with_llm_tiebreak` to enable LLM tie-breaking.
4. Tune per-scenario in the generated participant JSON under
   `situational_awareness.sa_reasoner` (node agents default to the fast
   `openai/gpt-oss-20b` + `low` effort for latency; the interactive chat defaults to
   `openai/gpt-oss-120b` + `medium` effort via `SMAC_CHAT_MODEL` /
   `SMAC_CHAT_REASONING_EFFORT`):

```json
{
  "groq_reasoning_agent": {
    "model": "openai/gpt-oss-20b",
    "reasoning_effort": "low",
    "temperature": 1,
    "top_p": 1,
    "max_completion_tokens": 2048,
    "timeout_s": 0.3,
    "invoke_every_n_rounds": 5,
    "invoke_on_tiebreak": true,
    "api_key_env_var": "GROQ_API_KEY"
  }
}
```

Implementation note: all Groq calls go through `nebula.core.smac_groq.groq_json_completion`,
which uses the official `groq` SDK (`AsyncGroq`, `reasoning_effort`, structured outputs,
`stream=False`) following Groq's documented usage, and falls back to the
OpenAI-compatible HTTPS endpoint via `httpx` on images without the SDK installed.

Check `https://console.groq.com/docs/models` before each deployment: the model catalog
changes often (`llama-3.1-8b-instant` was retired in favor of `openai/gpt-oss-20b` in
August 2026). The model name always comes from configuration, never hardcoded.

## Inter-node negotiation

Disabled by default (`sa_reasoner.negotiation.enabled: false`) so mixed-version
federations keep working. When enabled on all peers, `DISCONNECT` suggestions are only
executed on mutual `ACCEPT` over the `negotiation` protobuf message
(`PROPOSE`/`ACCEPT`/`REJECT`/`COUNTER_PROPOSAL`, 2-round TTL):

```json
{
  "negotiation": {
    "enabled": true,
    "timeout_rounds": 2,
    "wait_timeout_s": 5.0
  }
}
```

## Decision log (auditability)

Every arbitrated command is recorded (owner, action, priority, confidence, explanation,
round) in memory and appended to `app/logs/<scenario>/smac_decisions.jsonl`, which is
included in the dashboard *"Download logs"* archive.

## SMAC Panel (cognitive observability + control)

The dashboard offers **SMAC Chat** (`Compare Scenarios` → `SMAC Chat`, or the chat icon on
each scenario row): a **cognitive observability and interactive control panel**, not a
plain continuous chat. It is spatially split to correlate LLM reasoning with live
federation topology:

- **Header:** scenario loader, status badge, current round, active aggregation algorithm,
  node counts and auto-refresh toggle (5 s poll of `/platform/smac/state`).
- **Federation topology (live SVG):** ring layout with color-coded states (running,
  paused, offline/stopped, malicious) and orange rings for predicted stragglers.
  Clicking a node selects it and highlights its edges and related decisions.
- **Cognitive reasoning feed:** decision cards (owner, action, target badge, confidence
  bar, explanation, round) plus one-shot LLM analysis (`Analyze state` → State / Risks /
  Suggested next actions) via `/platform/smac/analyze`.
- **Control:** Pause/Resume node, switch aggregator, propose disconnect to a peer, stop
  scenario. Every control proposes a structured action that only runs after explicit
  **Confirm & run** (`/platform/smac/propose` + `/platform/smac/chat/confirm`).
- **Compact natural-language input** remains as a secondary channel (same propose/confirm
  flow under the hood).

| Action | Effect |
|---|---|
| `pause_node` / `resume_node` | Freeze/unfreeze the participant container (Docker pause, instant and lossless) |
| `select_aggregator` | Hot-swap the aggregation algorithm on the node(s) via the operator bus |
| `propose_disconnect` | Ask a node to open a negotiation to disconnect from a peer |
| `stop_scenario` | Stop the scenario through the controller API |

Flow: the assistant grounds its reply in a live snapshot (`scenario_config` vs `runtime`,
plus recent decisions) and returns structured action proposals with `sources`
("Based on: node 3 · round 10"); the panel shows **Confirm & run** / **Discard** buttons
and nothing executes without confirmation. Every request carries a `request_id` echoed
back (one Groq call at a time per scenario), and replies follow the operator's language.
Operator commands travel through `app/config/<scenario>/operator_commands/` files consumed
by the node-side `OperatorBus` (5 s poll, per-node result files, TTL expiry).

Security and ops (spec `SMAC-CHAT-SPEC.md`): session authentication on every route; RBAC
(`admin` full, `operator` everything but stop, `viewer` read-only); single-use expiring
confirmation tokens; reinforced confirmation for `stop_scenario` (type the scenario name);
per-user rate limit (20/min); audit trail in `app/logs/<scenario>/smac_audit.jsonl`;
deterministic template fallback when Groq is down; metrics at
`GET /platform/api/smac/metrics`; proactive alerts as SSE at
`GET /platform/api/smac/events?scenario=…` (`predicted_straggler`, `node_offline`,
`scenario_finished`…); unified execution at `POST /platform/api/smac/actions/execute`.

Requirements: `GROQ_API_KEY` exported before starting the platform (it is forwarded into
the frontend, controller and node containers automatically); without it the chat page
reports itself as disabled.

## API reference

::: nebula.core.situationalawareness.awareness.cognitive.cognitiveagent
::: nebula.core.situationalawareness.awareness.cognitive.groqreasoningagent
::: nebula.core.situationalawareness.awareness.cognitive.decisionlog
::: nebula.core.situationalawareness.awareness.stragglerpredictor.stragglerpredictor
::: nebula.core.situationalawareness.awareness.aggregationselector.aggregationselector
::: nebula.core.situationalawareness.awareness.resourcebroker.resourcebroker
::: nebula.core.situationalawareness.awareness.negotiation.negotiationmanager
::: nebula.core.situationalawareness.awareness.arbitrationpolicies.priorityllmtiebreak
::: nebula.core.smac_groq
::: nebula.core.operatorbus
::: nebula.frontend.smac_chat
