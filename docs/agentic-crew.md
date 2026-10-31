# Capa multiagente CrewAI (SMAC)

Capa **opt-in** de decisión para el controller/copiloto y el nodo agregador.
El crew solo propone: la confirmación humana, el RBAC y los guardrails
deterministas siguen siendo la última palabra.

## Activación

Controller (escenario `scenario.json`):

```json
{
  "adaptive_agent": {
    "multi_agent": true,
    "crew_strategy": "auto",
    "crew_require_human_review": true
  }
}
```

O por entorno:

```bash
export SMAC_MULTI_AGENT=1
export GEMINI_API_KEY=...
```

O desde la UI: **Deployment → Adaptive Agent → Multi-agent crew (CrewAI)**. El
toggle se serializa como `adaptive_agent.multi_agent` en `scenario.json`; el
resto de sub-banderas del copiloto siguen siendo independientes.

Nodo agregador (independiente del controller):

```bash
export SMAC_CREW_NODE_ENABLED=1
export SMAC_CREW_USE_CREWAI=0   # 1 solo si el grupo crewai está instalado
```

El nodo también puede activarse editando `crew_reasoning_agent.enabled` en el
`scenario.json` generado.

## Estrategias

| Estrategia | Uso | LLM |
|---|---|---|
| `fast_path` | una única sugerencia determinista | no |
| `voting` | defecto tras fast-path | sí (paralelo) |
| `role` | hallazgo crítico o >2 dominios | sí (+ coordinador) |
| `debate` | opt-in, máximo 2 turnos | sí (más caro) |
| `carry_over` | deadline/error | no |

`auto` elige fast-path → Voting → Role y deja Debate solo si
`crew_debate_enabled=true` y hay un hallazgo crítico.

## Límites

- Presupuesto total por decisión: `SMAC_CREW_TOTAL_TIMEOUT_S` (8 s por defecto).
- Caché por `state_hash`: `SMAC_CREW_CACHE_TTL_S` (45 s).
- Un máximo de `SMAC_CREW_MAX_AGENTS` especialistas (4 por defecto).
- Si el LLM falla o se agota el deadline se devuelve `carry_over` y el
  controller vuelve al flujo single-agent.
- Sin `GEMINI_API_KEY`/`GOOGLE_API_KEY` la capa se autodesactiva.
- Un reintento acotado ante `429/5xx` o timeout de transporte, solo si queda
  presupuesto (>1,5 s); nunca alarga el deadline total.

## Pipelining y métricas

`adaptive_watch` precalcula el `ContextPack` de los escenarios con
`multi_agent=true` y lanza el crew en background (`trigger=watch`) cuando cambia
el `state_hash` (respetando un intervalo mínimo) o cuando vence el TTL de
caché. Una petición manual posterior reutiliza la caché por `state_hash`, así el
razonamiento se solapa con la ronda en curso.

`GET /platform/api/smac/metrics` incluye un bloque `crew` con decisiones,
aciertos de caché, fallbacks, vetos, rate-limit, llamadas LLM, tokens de
entrada/salida y p50/p95 de latencia del crew. El crew expone esos contadores a
través de `record_metric` (`crew_*`), separados de la latencia del chat.

## Guardrails

Toda propuesta debe pasar:

1. whitelist de palancas implementadas en NEBULA;
2. evidencia trazable en el `ContextPack`;
3. coste para cambios estructurales/nueva simulación;
4. mínimo de clientes y ratio máximo de exclusión;
5. fairness (no excluir repetidamente al mismo nodo);
6. RBAC (solo admin/operator puede confirmar cambios).

Los patrones no implementados (HDH/GAN/JSD, compresión) están marcados
`available=false` y nunca pueden proponerse.

## Auditoría

- `crew_trace.jsonl` en el directorio de logs del escenario.
- `GET /platform/api/smac/crew/trace?scenario=<name>&limit=50`.
- Eventos de auditoría: `crew_proposed`, `ask_answered_crew`, `analyze_crew`.

## Dependencias

El grupo opcional `crewai` se instala solo en controller/frontend:

```bash
uv sync --group frontend --group crewai
uv sync --group controller --group crewai
```

Ventana fijada: `crewai>=0.100,<0.130` (las versiones >=0.130 exigen
`tokenizers>=0.20`, incompatible con `transformers==4.44.2` del grupo core).

## Variables principales

| Variable | Defecto | Efecto |
|---|---|---|
| `SMAC_MULTI_AGENT` | `0` | Activa el crew del controller. |
| `SMAC_CREW_STRATEGY` | `auto` | `auto`, `voting`, `role` o `debate`. |
| `SMAC_CREW_TOTAL_TIMEOUT_S` | `8` | Deadline duro por decisión. |
| `SMAC_CREW_CACHE_TTL_S` | `45` | Reutiliza la decisión para el mismo `state_hash`. |
| `SMAC_CREW_MAX_AGENTS` | `4` | Especialistas máximos por votación. |
| `SMAC_CREW_MAX_RPM` | `30` | Ventana global de llamadas LLM por minuto. |
| `SMAC_CREW_MIN_CLIENTS` | `2` | Clientes mínimos para excluir nodos (controller). |
| `SMAC_CREW_MAX_EXCLUSION_RATIO` | `0.4` | Fracción máxima de clientes excluibles. |
| `SMAC_CREW_USE_CREWAI` | `0` | Usa `CrewAI`; si falla, cae a Gemini directo. |
| `SMAC_CREW_NODE_ENABLED` | `0` | Activa `CrewReasoning` en cada nodo. |
| `SMAC_CREW_DEBATE_ENABLED` | `0` | Permite Debate en `auto`. |
| `SMAC_CREW_DEBATE_MAX_TURNS` | `2` | Turnos máximos de Debate (1–3). |

El nodo usa `min_clients=1` porque solo ve su vecindario; la fairness global se
valida en el controller.

## Evaluación

Resultados sintéticos con `--iterations 50` y sin clave de API (fast-path):

| Comando | Estrategia | p50 | p95 | Máx. |
|---|---|---|---|---|
| `benchmark_smac.py` | baseline single-agent | 0.026 ms | 0.044 ms | 0.138 ms |
| `benchmark_crew.py` | fast-path | 0.09 ms | 0.17 ms | 0.314 ms |

`benchmark_crew.py` también reporta la distribución de exclusiones. Con 5 nodos
y 50 iteraciones: 10 exclusiones por nodo y `max_exclusion_ratio=0.2`, sin sesgo
hacia un nodo concreto.

Estos números **no** incluyen latencia de red ni de Gemini: son la sobrecarga
determinista. Para comparar Voting/Role/Debate hay que ejecutar
`--live --iterations 5` con `GEMINI_API_KEY` y revisar `crew_trace.jsonl`.

Smoke test en vivo (2026-10-27, `gemini-3.5-flash-lite`, 1 especialista):

| Backend | Latencia | Resultado |
|---|---|---|
| Gemini REST directo | 1.53 s | `pause_node(n4)` válido |
| CrewAI 0.108.0 | 1.75 s | `pause_node(n4)` válido |

El smoke test valida el transporte y el esquema, no la calidad estadística. Para
CrewAI hay que instalar el grupo e iniciar con `SMAC_CREW_USE_CREWAI=1`.

## Runbook

Activar en un escenario de prueba:

```bash
export SMAC_MULTI_AGENT=1
export GEMINI_API_KEY=...
# opcional: SMAC_CREW_STRATEGY=voting
```

Verificar:

```bash
curl -s 'http://localhost:8000/platform/api/smac/crew/trace?scenario=demo&limit=5'   -H 'Cookie: session=...' | jq '.traces[0]'
```

Criterios de rollback:

1. `p95` del endpoint de chat > 8 s de forma sostenida.
2. Aumento de `crew_hold` o `fallbacks` sin mejora de propuestas.
3. Cualquier propuesta fuera de la whitelist (debe ser imposible).
4. Quejas de fairness: un nodo excluido por encima de
   `SMAC_CREW_MAX_EXCLUSION_RATIO`.

Rollback inmediato: `SMAC_MULTI_AGENT=0` en el controller y
`SMAC_CREW_NODE_ENABLED=0` en los nodos. El controller relee `scenario.json` en la
siguiente petición; los nodos ya lanzados necesitan reiniciarse para aplicar el
cambio de su configuración.

## Diagnóstico

```bash
python scripts/crewai_spike.py --scenario <name> --offline
python scripts/benchmark_smac.py --iterations 200
python scripts/benchmark_crew.py --iterations 50
python scripts/crewai_spike.py --scenario <name> --live   # requiere API key
```

## Límites actuales

- El chat solo invoca el crew cuando `is_decision_intent()` detecta una petición
  de decisión; el resto de preguntas sigue por el flujo single-agent.
- `adaptive_watch` sigue encargándose únicamente de las pausas por sugerencias.
  No lanza el crew en cada poll: la invocación es explícita desde chat o
  `handle_analyze`, para no generar coste ni propuestas sin contexto humano.
- `exec_interpreter` no expone un flag crew propio. La activación vive en
  `scenario.json :: adaptive_agent.multi_agent` o en `SMAC_MULTI_AGENT`.
