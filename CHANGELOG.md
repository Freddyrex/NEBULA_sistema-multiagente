# Changelog
All notable changes to this project will be documented in this file.

## Botones de navegación en el chat del agente

El agente prometía redirecciones ("Te redirijo al panel...") sin mostrar ningún
botón: el prompt solo autorizaba `navigate` para crear un escenario, el esquema
declaraba `params` como objeto libre (Gemini devolvía `params: {}`) y la
allowlist tenía un único destino. Ahora el chat ofrece botones hacia cualquier
página de NEBULA, sin listas escritas a mano.

- `nebula/frontend/pages.py` (nuevo): `build_page_catalog(app)` deriva el
  catálogo de páginas de las rutas reales de la app — solo las GET declaradas
  con `response_class=HTMLResponse` (las de API devuelven JSON y quedan fuera
  por construcción) y de las que el backend puede completar. Cada página lleva
  id (derivado de la ruta), `purpose` (primera frase del docstring del
  handler) y sus parámetros.
- `nebula/frontend/smac_chat.py`: el prompt describe el catálogo `pages` que
  llega en el contexto y exige que `target` sea el id de la página que
  corresponde a lo que pidió el operador; prohíbe prometer un redireccionamiento
  sin emitir la acción `navigate`.
- `nebula/frontend/smac_chat.py`: `ACTION_PARAMS_SCHEMA` declara las claves
  admitidas en `params` (con `target` incluido) — con `params` libre Gemini
  devolvía `{}` y el botón nunca se generaba.
- `nebula/frontend/smac_api.py`: `SmacDeps.pages` lleva el catálogo;
  `_llm_navigate_actions()` resuelve el destino contra el catálogo (descarta
  destinos inventados y URLs ajenas, deduplica) y la etiqueta del botón la
  escribe el modelo en el idioma del operador. El único caso determinista es la
  creación de escenario (comportamiento previo): **la intención la detecta el
  modelo**, no una lista de frases.
- `nebula/frontend/pages.py`: `find_page()` resuelve de forma tolerante (id
  exacto → normalizado → sufijo → palabras coincidentes con el id) para que el
  modelo pueda escribir `statistics`, `statistics page` o la ruta; un id
  inventado no casa con ninguna página.
- `nebula/frontend/static/js/nb-chat-float.js`: los destinos de una misma
  respuesta se agrupan en una tarjeta con una fila de botones (`addNavCard`).
- Seguridad: solo URLs internas relativas construidas en el servidor a partir
  de las rutas de la app; el escenario se sanitiza; las URLs externas se
  descartan con warning.
- Tests: `tests/test_smac_nav_targets.py` (12 casos: catálogo derivado de las
  rutas, resolución tolerante, allowlist, deduplicación, roles y rechazo de
  URLs externas).

## Tono más natural en el chat del agente

Las respuestas del copiloto sonaban a informe estructurado (encabezados, negritas
y volcados de métricas). Ahora:

- `CHAT_SYSTEM_PROMPT`: guía de estilo conversacional (respuesta directa primero,
  prosa breve, sin markdown, métricas dentro de la frase, sin preámbulos ni
  cierres de cortesía, ~120 palabras salvo que se pida detalle).
- `ANALYZE_SYSTEM_PROMPT`: informe con etiquetas planas (`State:`, `Risks:`,
  `Suggested next actions:`) en vez de negritas markdown.
- `ask_gemini()`: `_naturalize_reply()` elimina restos de `**negritas**` y
  encabezados `#` que el chat muestra como texto plano.
- `decision_reply()` (crew): propuestas redactadas como recomendación de un
  colega ("Mi propuesta: … Si te encaja, lo aplicamos") y detalles por tipo de
  acción en el idioma del operador.

## Corrección — errores del chat ya no se muestran como fallo de conexión

El chat flotante intentaba `res.json()` sobre respuestas HTML/texto plano
(404/405/413 del handler de errores y 500 de Starlette), y el usuario veía
`Could not reach the agent. JSON.parse: unexpected character...`.

- `app.py`: el handler de `HTTPException` devuelve JSON para rutas
  `/platform/api/*` y `/platform/smac/*` (o `Accept: application/json`);
  las páginas de navegación siguen mostrando HTML. Nuevo handler de `Exception`
  que devuelve un 500 JSON en rutas de API y registra el traceback.
- `nb-chat-float.js`: helper `readJson` que lee el cuerpo como texto, parsea con
  `try/catch` y muestra el `status` + mensaje real; se aplica a chat, confirmar
  y proponer. El 429 muestra un aviso de rate-limit en vez de “fallo de conexión”.

## Capa multiagente CrewAI para SMAC (opt-in)

Nueva capa de decisión multiagente (controller/copiloto y nodo agregador),
apagada por defecto y con el crew limitado a **proponer** cambios.

- `nebula/agentic/`: contratos, contexto redactado, registro de palancas reales,
  prompts, backend Gemini de una llamada, adaptador CrewAI opcional,
  guardrails deterministas, estrategias (`fast_path`, `voting`, `role`,
  `debate` opt-in, `carry_over`) y trazas `crew_trace.jsonl`.
- Presupuesto p95 de 8 s, cache por `state_hash`, rate limit global
  `SMAC_CREW_MAX_RPM`, fallback al flujo single-agent si el LLM falla y veto de
  guardrails (whitelist, evidencia, coste, mínimo de clientes, fairness y RBAC).
- `benchmark_crew.py` reporta latencia, estrategia y distribución de exclusiones
  para revisar fairness; resultados sintéticos y límites en `docs/agentic-crew.md`.
- Verificado en vivo con Gemini (`gemini-3.5-flash-lite`): backend REST directo y
  CrewAI 0.108.0 proponen `pause_node` en ~1.5–1.8 s. Se corrigió el sanitizador
  de esquemas (propiedades llamadas `pattern`) y se restringió el enum a patrones
  disponibles; los cambios planos (`kind/total_rounds`) se normalizan a `change`.
- El orquestador se reutiliza por escenario/configuración, de modo que la caché
  por `state_hash` sobrevive entre peticiones; las propuestas duplicadas reutilizan
  el mismo `action_id` pendiente.
- Controller: integración en `handle_analyze`/`handle_ask`, endpoint
  `GET /platform/api/smac/crew/trace` y racional por agente en el chat.
- UI: interruptor **Multi-agent crew (CrewAI)** en Deployment → Adaptive Agent,
  serializado como `adaptive_agent.multi_agent` en `scenario.json`.
- Nodo: `CrewReasoning` mapea la propuesta a `SACommand`; la arbitración y el
  fallback determinista no cambian. Flag independiente y off por defecto.
- Dependencia opcional `crewai` (ventana `>=0.100,<0.130`); sin ella se usa el
  backend Gemini directo. Docker de controller/frontend instala el grupo.
- ADR `docs/adr/0001-smac-crewai-multiagent.md`, scripts de spike y benchmark,
  y tests de contratos, orquestador, nodo e integración.
- `adaptive_watch` precalcula el `ContextPack` y lanza el crew en background
  cuando cambia el `state_hash` (pipelining, plan §3.4/§11.5).
- Métricas `crew_*` en `/platform/api/smac/metrics`: decisiones, caché,
  fallbacks, vetos, rate-limit, llamadas LLM, tokens de entrada/salida y
  p50/p95 de latencia del crew.
- Un reintento acotado ante `429/5xx`/timeout de transporte, solo con
  presupuesto restante, sin alargar el deadline total.

## Sprint 1 — Métricas completas (ROADMAP_AGENTE_ADAPTATIVO.md)

Agente SMAC ahora ve accuracy, F1 macro, recall macro, precision macro, RAM, CPU y
temperatura por nodo y ronda (fuente única: `MetricsService`).

- `tags.yaml` `schema_version` 1.0 → **1.1** (+ `f1_macro`, `recall_macro`,
  `precision_macro`, `cpu_percent`, `ram_percent`, `cpu_temp`).
- `providers.py`: `TAG_MAP` mapea `Test (Local)/F1Score|Recall|Precision`
  (`step_is_round`) y `W-CPU/CPU global (%)`, `Z-RAM/RAM global (%)`,
  `W-CPU/CPU temperature (°)` (`around_map`); el centinela `0` de corridas
  viejas sin sensor se descarta (regla 5: nada inventado); CPU/RAM canónicos
  siguen alimentando el bucket ARX; sin sensor se avisa
  "cpu_temp no disponible; cpu_temp_est es estimado (ARX)".
- `reporter.py`: sin sensor de temp el tag `W-CPU/CPU temperature (°)` se
  **omite** (antes se logueaba `0`); `SystemMonitor.get_temperature()` nuevo
  (sensor real o `None`, nunca inventa) e incluido en `get_system_resources()`.
- `digest.py`/`service.py`/`defaults.py`: nuevas métricas con
  `first/final/delta/best/converged_at` automáticos.
- `smac_chat.py` (prompts + fallback determinista) y SMAC Panel
  (`smac-chat.js`, `smac_chat.html`): KPIs/curvas de las 6 nuevas; `*_est`
  etiquetado "(estimado ARX)"; sin dato → "no disponible".
- Tests: `tests/test_metrics_sprint1.py` (sintético 3 nodos × 5 rondas).
- Aditivo: sin cambios en escenarios existentes; sin dependencia de Groq.

## Sprint 2 — Intérprete de ejecución (ROADMAP_AGENTE_ADAPTATIVO.md)

De números a hallazgos tipados con evidencia, sin LLM (determinista).

- Nuevo `nebula/frontend/exec_interpreter.py`: `analyze_digest()` traduce el
  digest en `Finding {id, type, severity, metric, nodes[], evidence[], summary}`.
  Reglas: `accuracy_plateau`, `f1_recall_gap`, `low_recall`, `ram_pressure`,
  `cpu_pressure`, `thermal_throttle_risk`, `straggler`, `byzantine_suspected`,
  `over_provisioned_rounds`. Sin datos suficientes la regla no dispara (regla 5:
  nada inventado); lo estimado va etiquetado `estimated`.
- Nuevo `nebula/frontend/interpreter_thresholds.yaml`: todos los umbrales
  configurables (mínimo de rondas, eps/k de plateau, brechas, límites de
  RAM/CPU/temperatura, ratio de stragglers, rondas extra). Sin el YAML se usa
  el fallback en código.
- Bandera `adaptive_agent.exec_interpreter` en `scenario.json` (apagada por
  defecto, regla 3). `MetricsService.summary(..., findings_enabled)` la respeta;
  la caché incluye flag + mtime de umbrales. Endpoint `/summary` y snapshot SMAC
  la leen de `scenario.json`.
- `build_panel_state()` expone `findings[]` (top-level) y el snapshot del LLM
  los incluye (`context["findings"]`); prompts actualizados ("explica ESTOS
  hallazgos; no inventes otros"); `deterministic_reply` los lista sin Groq.
- SMAC Panel: tarjeta "Hallazgos" (`renderFindings()` desde el state, sin Groq)
  con severidad/info/evidencias; `findings_count` en el contexto del chat.
- Stragglers a nivel digest (misma familia de ratio que `StragglerPredictor`,
  cuya predicción por latencias sigue en vivo en el bucle de entrenamiento);
  `byzantine_suspected` es proxy por outliers de última ronda (la varianza real
  de updates llega en el Sprint 3).
- Tests: `tests/test_exec_interpreter_sprint2.py` (16 tests: cada regla dispara
  y no dispara, evidencia verificable, override de umbrales, flag off por
  defecto, prompt + fallback sin Groq).
- Aditivo: `findings: []` por defecto; sin dependencia de Groq.

## Sprint 3 — Cambio dinámico de agregador (ROADMAP_AGENTE_ADAPTATIVO.md)

El agente sugiere (y, si se acepta, ejecuta) el cambio de agregador con métricas.

- Política determinista en `exec_interpreter.suggest_aggregator()` (opción
  "política en el intérprete": el `AggregationSelector` en vivo no se toca):
  bizantina → Krum, stragglers → TrimmedMean, limpio → FedAvg. Con objetivo ya
  activo, algoritmo desconocido o ventana de cooldown: sin sugerencia.
- Histéresis + cooldown (`aggregator_policy` en `interpreter_thresholds.yaml`,
  `cooldown_rounds: 3`): lee `smac_audit.jsonl` (último `select_aggregator`
  ejecutado + rechazos del mismo objetivo enlazados por id de propuesta).
  `current_aggregator()` distingue declarado (DB) vs runtime (auditoría).
- Banderas `adaptive_agent.{aggregator_suggestions, aggregator_auto}` en
  `scenario.json` (apagadas por defecto); `Scenario` del controller acepta
  `adaptive_agent` (antes rompía `from_dict`). `aggregator_auto` reservado:
  `auto_allowed()` siempre False en Sprint 3 (riesgo 3 del roadmap).
- Flujo existente reutilizado sin cambios: sugerencia → `/propose`
  (`params` con `trigger`+`previous`) → tarjeta pendiente Aceptar/Rechazar →
  `OperatorBus._do_select_aggregator` (hot-swap verificado).
- Auditoría extendida (aditiva): `action_proposed` += ronda; `action_executed`
  += `params` + ronda (traza métrica→prev/new→resultado); `action_rejected` +=
  `kind`+`params`.
- Panel: tarjeta "Sugerencias" (`Cambiar FedAvg → Krum, porque…` + Proponer);
  `aggregator_policy` en el state; prompts + `deterministic_reply` la cubren.
- Tests: `tests/test_aggregator_policy_sprint3.py` (13 tests: prioridades,
  noop, cooldown/supresión por rechazo, hot-swap real con engine falso,
  traza propose→reject en `smac_audit.jsonl`, flag en `Scenario`, prompt).
- Aditivo: `suggestions: []` por defecto; sin Groq todo funciona.

## Sprint 4 — Costo de cambiar de arquitectura (ROADMAP_AGENTE_ADAPTATIVO.md)

Ningún cambio de arquitectura se sugiere sin su costo (5 rubros etiquetados).

- Nuevo `nebula/frontend/cost_model.py` (puro, determinista, sin Groq):
  `model_size_mb()` mide parámetros/MB instanciando en CPU sin datos (mismo
  mapa que `node.py`; verificado: MNIST MLP 235146 params/0.90MB, CNN 6.5M/
  24.8MB), `classify_continuity()` provisional (Sprint 6 lo reemplaza con
  `can_resume`), `estimate_cost()` con compute/resources/network/transition/
  benefit (beneficio siempre como rango {low,mid,high} + método + basis, nunca
  un número solo), `decide()` multi-criterio (recursos bajo límites +
  transición asumible + ganancia ≥ min_gain con historial; si no, "no conviene"
  explícito). Todo valor: measured|projected|hypothesis|estimated; lo que falta
  es unavailable, nunca inventado.
- Nueva acción `propose_architecture_change {change, cost}` (whitelist; RBAC
  admin/operator; viewer bloqueado). Guard backend en `/propose`: sin costo
  completo → 400; declarado sobre límites → 400; recálculo independiente con
  medidos que exceda → 400 aunque lo declarado pase; sin medidos se acepta
  marcado `unverified`. Ejecución diferida (registra + audita; la mutación real
  `adjust_rounds`/`swap_model` llega en Sprint 5).
- Auditoría: trigger/previous/new viajan en `params` (traza métrica→cambio→
  resultado); rechazos del guard auditados con `guard: cost-model`.
- Panel: `pendingCard` renderiza tabla costo/beneficio + alternativa "seguir
  igual"; prompt del LLM exige cost de 5 rubros.
- Umbrales `cost_model` en `interpreter_thresholds.yaml`.
- Tests: `tests/test_cost_model_sprint4.py` (10 pasan + 1 skip: casos conviene/
  no-conviene, matriz de continuidad, backend que rechaza aunque el LLM pase,
  guard end-to-end 400, RBAC + ejecución diferida).
- Aditivo: sin cambios en el entrenamiento; sin Groq todo funciona.

## Sprint 5 — Arquitectura según las rondas (ROADMAP_AGENTE_ADAPTATIVO.md)

Ajustar la simulación según el avance, con costo y confirmación siempre.

- Nodo: `adjust_rounds {total_rounds}` (actualiza `engine.total_rounds` bajo
  `round_lock` + sincroniza el config; el `while` lo recoge en la siguiente
  cabecera: la ronda en curso siempre termina), `adjust_epochs {epochs}`
  (próxima ronda), `swap_model {to_model}` (solo mismo dataset, pesos frescos,
  rechaza a mitad de ronda e incompatibles con mensaje claro; reconstruye el
  trainer y purga agregaciones pendientes). `batch_size` se rechaza en ejecución
  (exige rebuild del dataloader: nueva ejecución). Whitelist
  `EDITABLE_CHANGE_FIELDS` enforced en el guard.
- Broadcast "all": `collect_command_results()` + `execute_operator_action`
  recoge por nodo hasta `OPERATOR_RESULT_WAIT_S`; sin respuesta en el TTL el
  cambio queda `partial` con ALERTA (nunca se asume éxito).
- `propose_architecture_change` ahora ejecuta de verdad (rounds→adjust_rounds,
  epochs→adjust_epochs, model→swap_model, aggregator→select_aggregator).
- Intérprete `suggest_architecture()`: plateau→recorte con ahorro cuantificado,
  mejora fuerte al final→ampliación, divergencia→menos epochs; candidatos de
  modelo solo con hipótesis explícita (si no, `rejected[]` con "no conviene").
  Todo por `estimate_cost`+`decide`. Matriz de compatibilidad documentada en
  `nebula/core/models/registry.py` (fuente única; `cost_model` la reutiliza).
- Panel: sugerencias de arquitectura con tabla costo/beneficio + rechazados
  ("no conviene: motivos"); prompts y fallback sin Groq actualizados.
- Tests: `tests/test_architecture_sprint5.py` (13 tests: políticas, hot-swap
  real con engine falso, parcial broadcast, whitelist/compat en guard).
- Aditivo: bandera `adaptive_agent.architecture_suggestions` (off); sin Groq OK.

## Sprint 6 — Continuar de una ejecución a otra (ROADMAP_AGENTE_ADAPTATIVO.md)

Reanudar con datos y mecanismo: checkpoints, matriz de continuidad, linaje.

- Nuevo `nebula/core/checkpoint.py`: `save/validate/load/list` por nodo y ronda
  en `<config_dir>/<scenario>/checkpoints/` (weights.pt + optim.pt opcional +
  meta.json con sha256, formas, config y métricas). Íntegro o explícito:
  corrupto/incompatible falla con mensaje, sin arrancar en silencio.
- Nodo: reporter guarda cada N rondas (`checkpoint_args`, off si ausente);
  pre-checkpoint antes de `swap_model`; arranque con `resume_from` hace warm
  start (strict mismo modelo, parcial si no) o ValueError sin entrenar.
  Optimizador: se guarda si aplica; en arranque hay optimizer fresco (explícito).
- `Scenario` acepta `parent_scenario/checkpoint_args/resume_from` (patrón
  adaptive_agent, sin migrar DB) y los propaga a los participants.
- `cost_model.can_resume()` (matriz full/partial/none + capas reutilizadas vs
  reiniciadas + `lost_rounds_estimate`) alimenta el costo de transición.
- Acción `resume_from {parent, checkpoint_round, changes?}`: guard exige
  checkpoint válido en TODOS los nodos + cambios whitelist + modelo compatible;
  ejecuta creando la hija por `POST /scenarios/run` con enlace guardado.
- Métricas continuas: `MetricsService` con `config_dir` resuelve `lineage`
  (cadena padre→hijo, anti-bucles) y `series()` cose padre (podado) + hija
  (offset) con puntos de cambio marcados; digest describe la hija + linaje.
- Panel: tarjeta Linaje + fallback sin Groq; prompts la citan como historia.
- Tests: `tests/test_continuity_sprint6.py` (14 tests: roundtrip/estricto/
  parcial/corrupto/poda, resume full+parcial+corrupto, matriz can_resume,
  guard 400, POST hija, linaje anti-bucles, stitching).
- Aditivo y sin Groq. Límite honesto: el contador de rondas reinicia en la hija
  (el offset global vive en el linaje); restaurar estado de optimizador
  pre-fit queda explícitamente pendiente.

## Sprint 7 — Integración, seguridad y evaluación (ROADMAP_AGENTE_ADAPTATIVO.md)

Cierre del flujo hallazgo → sugerencia (con costo) → aceptar → ejecutar →
verificar → registrar.

- Auditoría completa: `_execute_record` adjunta `projected_cost` (el costo
  guardado de la propuesta, o nota heurística) + `baseline` (finales medidos
  best-effort) + resultado a cada `action_executed`.
- Verificación a +K rondas (`verification.k_rounds: 3`, configurable):
  `due_verifications()` (pending/ready desde la auditoría) y
  `verify_execution()` (ganancia actual vs banda proyectada + nota de
  calibración como reporte, sin auto-ajuste). Expuestas en el state
  (`verifications`), tarjeta del panel, prompts y fallback sin Groq.
- Informe final `NEBULA_final_report.md` autogenerado al completar (decisiones
  con costos, proyectado vs real, linaje, hallazgos); se regenera si las
  métricas son más nuevas.
- Seguridad: matriz RBAC verificada para las 7 acciones (admin/operator;
  `stop_scenario` solo admin; viewer lectura); Groq-down probado (degradado +
  botones manuales intactos, sin Groq en el path de ejecución).
- E2E en cadena (3 nodos sintéticos): plateau → recorte con ahorro → propone →
  acepta → ejecuta → verifica en banda. Sin Docker aquí (disco al 99%): el
  E2E vivo con ataque/straggler/RAM/plateau queda como checklist manual en el
  anexo de docs; la cadena completa está automatizada.
- Docs: `SMAC_comunicacion_y_nueva_API.md` PARTE 3 (sprints 1–7 + decisiones
  §5 resueltas + activación por escenario).
- Tests: `tests/test_integration_sprint7.py` (9 tests: RBAC, Groq-down, dues,
  bandas, costo en auditoría, E2E, informe, prompts/docs).
- Suite total: 104 passed + 1 skip. Sin Groq todo funciona.

## Pausa automática durante sugerencias (post-Sprint 7)

La simulación se pausa temporalmente mientras el agente sugiere una mejora.

- Al aparecer sugerencias (agregador o arquitectura) con el escenario en
  ejecución: `pause(all)` del operator bus (`engine.operator_paused`, el ciclo
  se detiene entre rondas) + auditoría `auto_paused`. Una sola vez por
  sugerencia (idempotente); con nodos ya pausados manualmente no se marca nada.
- Al decidir (aceptar O rechazar) se reanuda (`auto_resumed`); si las
  sugerencias se liquidan solas, también; botón Reanudar manual
  (`POST /platform/smac/resume`, operator/admin) como override.
- Las pausas manuales nunca se tocan (sin marcador, sin reanudación ajena).
- Panel: banner en Sugerencias + flag `auto_paused` en el state; prompts y
  fallback lo mencionan.
- Tests: `tests/test_autopause.py` (6 tests).
- Suite total: 110 passed + 1 skip.

## Chat → página de nuevo escenario

Pedir un escenario nuevo por chat lleva a su página de creación.

- `wants_new_scenario()` (ES/EN, verbos de creación; "el escenario nuevo va
  lento" no matchea) + `NEW_SCENARIO_URL = /platform/dashboard/deployment/`.
- `handle_ask` añade acción `navigate` (sin registro ni confirmación: navegar
  no es mutación, vale para viewer) + línea con el enlace, con y sin Groq.
- El chat flotante la renderiza como botón directo; el prompt prohíbe al LLM
  inventar otras URLs.
- Tests: `tests/test_navigate_new_scenario.py` (4 tests).
- Suite total: 114 passed + 1 skip.

## Chat sin escenario: crear lleva a la página (fix)

"Hola, creemos un escenario" ya no rebota con "I need a scenario".

- `wants_new_scenario()` + "creemos", "vamos a crear", "let's create".
- `handle_ask` sin escenario atiende solo la creación (botón + enlace, sin
  snapshot); lo demás sigue exigiendo escenario.
- El chat flotante deja enviar sin escenario abierto cuando es creación
  (espejo local; el backend revalida).
- Tests: 5 en `test_navigate_new_scenario.py`.
- Suite total: 115 passed + 1 skip.

## Comprensión LLM de la intención de crear (sin hardcode)

El agente comprende; el regex es solo fallback sin Groq.

- El system prompt ordena proponer `navigate` con la URL exacta de creación;
  `handle_ask` deja pasar las del LLM validadas contra allowlist (el resto se
  descarta: anti open-redirect) sin registrarlas (sin confirmación).
- El fallback determinista solo actúa si el LLM no propuso navegar (o sin
  Groq / sin escenario). Sinónimos de simulación incluidos.
- Tests: 6 en `test_navigate_new_scenario.py` (comprensión, URL maligna,
  no-duplicado).
- Suite total: 116 passed + 1 skip.

## Incidente en vivo: nodos caídos + Groq degradado + chat con sugerencias

- Nodos del escenario muertos sin dejar rastro (sin contenedores, sin logs
  nuevos, red vacía): el broadcast Krum fue al vacío (0 ok, parcial honesto).
  Acción: relanzar el escenario desde el dashboard.
- Groq degradado por doble causa: clave actual inválida (401) + 413 reales en
  logs con la anterior. `resolve_groq_key()` (env → fichero → "") y guard
  anti-413 (`shrink_snapshot_for_llm`, cap 24KB, sin mutar caché).
  Para reactivar: pegar clave válida en `/nebula/apikey` (o `GROQ_APIKEY_FILE`)
  y reiniciar el frontend.
- Sugerencias y pausa dentro del chat flotante (sin ir al panel): el panel
  publica sugerencias compactas + `auto_paused` en el contexto; el chat las
  anuncia una vez (con aviso si cerrado), botón Proponer (flujo existente) y
  banner de pausa con Reanudar.
- Tests: `tests/test_groq_resilience.py` (3 tests).
- Suite total: 119 passed + 1 skip.

## Sin hot-swaps en ejecuciones finalizadas (fix)

El agente sugería cambiar a Krum con 10/10 rondas y nodos caídos (broadcast al
vacío). Ahora `suggest_aggregator()` y `suggest_architecture()` reciben
`scenario_completed` (por estado o rondas) y no sugieren cambios en caliente;
arquitectura devuelve `rejected: usa resume_from`. Tests en S3/S5.
- Suite total: 121 passed + 1 skip.

## Deploy en vivo (fix)

Los contenedores sirven `/nebula` por bind-mount pero con Python cargado al
arranque: tras reiniciar frontend+controller el backend lleva el código actual
(verificado: intent + URL en vivo, JS nuevo servido, deployment 200).
`STATIC_VERSION` → 20261012 para romper la caché del navegador (si ves JS
viejo: Ctrl+Mayús+R).

## Agente SMAC tipo Copilot — Fase 1: activar el agente desde la UI

Interruptor visible en la pantalla de creación (antes solo editando
`scenario.json` a mano).

- `templates/deployment.html`: nueva sección "Adaptive Agent (SMAC)" con
  interruptor maestro + sub-opciones (todas off por defecto): analizar la
  ejecución, sugerir agregador, sugerir arquitectura, **pausar por sugerencia
  o nodo deficiente** (`pause_on_suggestion`, default on con el maestro) y el
  reservado `aggregator_auto`.
- `static/js/deployment/adaptive-agent.js` (nuevo, patrón `dp.js`):
  `AdaptiveAgentManager` con `get/set/reset`; el maestro OFF colapsa todas las
  banderas a false.
- `static/js/deployment/scenario.js`: `collectScenarioData()` serializa
  `adaptive_agent`; `loadScenarioData()` lo restaura; `clearFields()` lo
  resetea. Sin esto el agente se perdía al guardar/cargar/relanzar.
- `static/js/deployment/main.js`: import + init + `window.AdaptiveAgentManager`.
- `exec_interpreter.adaptive_flags()`: nueva clave `pause_on_suggestion`
  (default contextual: sin bloque `adaptive_agent` se conserva la auto-pausa
  legada; con bloque editado a mano sin la clave → off, opt-in).
- `smac_api.maybe_auto_pause()`: respeta `pause_on_suggestion` (un escenario
  viejo con la clave ausente no se auto-pausa); limpia marcadores huérfanos
  si la bandera está off.
- `STATIC_VERSION` → 20261020.
- Tests: `tests/test_adaptive_flags_ui.py` (serialización, default contextual,
  compatibilidad hacia atrás, no auto-pausa con la bandera off).

## Agente SMAC tipo Copilot — Fase 2: hallazgo → sugerencia y pausa sin panel

Un hallazgo de "nodo deficiente" genera una **sugerencia accionable** (sin LLM)
y la pausa ocurre aunque el panel no esté abierto.

- `exec_interpreter.suggest_from_findings()` (nueva, pura, sin LLM): mapa
  hallazgo → sugerencia con `id` estable, `class` (`hot`|`structural`),
  `finding_ids`, `nodes`, `reason`, `evidence[]` y el `change`. Cubre
  `ram_pressure`/`cpu_pressure`/`thermal_throttle_risk` → `batch_size` menor
  (structural), `straggler` por topología → re-topologizar, `f1_recall_gap`/
  `low_recall` → federación **solo con hipótesis explícita** (si no, `rejected[]`
  con "no conviene"). Los cambios hot de agregador siguen delegados a
  `suggest_aggregator` (Sprint 3) para no duplicar tarjetas.
- `interpreter_thresholds.yaml`: nuevos umbrales bajo `suggest_from_findings`
  (agregadores objetivo, `topology_min_neighbors`, `topology_target`,
  `pressure_batch_size`, `low_recall_federation`), sin hardcodear.
- `cost_model`: `EDITABLE_CHANGE_FIELDS` extendido con los campos estructurales
  (`federation`/`topology`/`n_nodes`/`sar_neighbor_policy`) y validación por
  tipo en `_check_change_structure` (federación ∈ {DFL,CFL,SDFL}; topología ∈
  {Fully,Ring,Star,Random}; SA ∈ {Distance,Random,Closest,Farthest}; `n_nodes`
  1..1000). `classify_change()` nuevo (puro, hot vs structural).
  `classify_continuity`/`estimate_cost` tratan los cambios estructurales como
  nueva ejecución (recursos/red `unavailable`, sin proyectar la topología
  actual sobre otra).
- `smac_chat.build_chat_context()`: fusiona `suggest_from_findings` en
  `architecture.suggestions` (deduplicado por `id`), con contexto de topología
  (`_neighbors_by_node`, `iid`, `federation`). `_suggestion_titles()` ya lo
  incluía por vivir en `architecture.suggestions`.
- `smac_api.maybe_auto_pause()`: `auto_paused` ahora audita `suggestion_ids`,
  `finding_ids` y la ronda.
- `app.py`: watcher de fondo `adaptive_watch()` (`asyncio.create_task`, patrón
  `monitor_resources`): cada `ADAPTIVE_WATCH_POLL_S` recorre los escenarios en
  ejecución con `pause_on_suggestion` y llama `get_panel_state(use_cache=False)`
  (dispara `maybe_auto_pause` sin el panel abierto). Tolerante a fallos.
- `defaults.py`: `ADAPTIVE_WATCH_POLL_S` (env `SMAC_ADAPTIVE_WATCH_POLL_S`, 10 s).
- Tests: `tests/test_suggest_from_findings_fase2.py` (mapa, rejected sin
  hipótesis, cooldown, `scenario_completed`, clasificación y whitelist
  estructural, helpers de ids).

## Agente SMAC tipo Copilot — Fase 3: un clic y cambio de agregador en caliente (parcial)

La tarjeta de sugerencia pasa a ser **una sola** con Aceptar/Rechazar y
"antes→después", en ambos chats (flotante y panel), y un cambio que no llega a
todos los nodos **no reanuda en silencio**.

- `smac_api.handle_confirm`: si la ejecución devuelve `partial` (no todos los
  nodos aplicaron el cambio), **no** se limpia el marcador de auto-pausa: se
  audita `auto_resume_skipped` (`reason: "partial change"`) y la respuesta trae
  `partial: True`, `auto_resumed: False` y `detail` con "cambio PARCIAL". Si el
  cambio es completo, reanuda como antes (`auto_resumed`).
- `nb-chat-float.js` / `smac-chat.js`: `maybeShowSuggestions`/`renderSuggestions`
  reescritas a tarjeta única con **antes→después**, evidencia y riesgo; el botón
  **Aceptar** encadena `propose`→`confirm` con el token single-use existente
  (sin paso intermedio visible) y **Rechazar** reanuda (`/platform/smac/resume`).
  `confirmAction` muestra el resultado **por nodo** y avisa cuando el cambio fue
  parcial.
- Rol: `window.NebulaRole` se inyecta en `layout.html` (todas las páginas) y en
  la ruta de `smac_chat`. Un rol no-admin/operator ve la **tarjeta informativa
  sin botones** (`needsConfirm`); el backend ya devuelve 403 al proponer.
- i18n: nuevas claves en `es.json`/`en.json` (`accept`, `decline`, `suggestionWhy`,
  `before`/`after`/`beforeAfter`, `risk`, `keepAsIs`, `needsConfirm`, `partialWarn`,
  `resumedOk`, `resumeAsk`, `perNode`, `nodesMissing`, `evaluated`).
- CSS: `.nb-action__line` (+ `text-warning`/`text-danger`/`text-success`) en
  `nebula-governance.css`.
- `defaults.py`: `STATIC_VERSION` → `20261021`.
- Tests: `tests/test_aggregator_hot_swap_fase3.py` (parcial no reanuda + audita,
  completo reanuda, rechazar reanuda, viewer 403).

## Agente SMAC tipo Copilot — Fase 4: cambio estructural ⇒ nueva simulación precargada (backend)

Un cambio de arquitectura **estructural** (federación, topología/nº de nodos,
modelo no hot-swappable, `batch_size`, políticas SA) ya no se fuerza en vivo:
al aceptar, SMAC crea un **borrador** y devuelve la URL de la pantalla de
creación (`?draft=<id>`). La simulación original **queda pausada e intacta**.

- `smac_chat.ACTION_KINDS`: nueva `propose_new_simulation`
  (`params = {parent, changes, cost, explanation}`); operator/admin pueden
  ejecutarla (stop_scenario sigue siendo solo admin).
- `smac_chat.build_draft_scenario()` (nueva, **pura y testeable**): copia solo
  la whitelist §3.2 (`DRAFT_WHITELIST`), aplica los cambios estructurales, pone
  título `"<título> (arq. v2)"` y añade linaje `derived_from`
  `{scenario, round, changes, created_by}` + `parent_scenario`. En cambio de
  topología/nº de nodos **suelta** `nodes`/`nodes_graph`, emite `topology_hint`
  `{type, n_nodes}` y lista `lost_node_attributes` (malicious/ip/… ) — nunca en
  silencio. `resume_from` solo si el llamador lo pasa explícitamente (por
  defecto arranca desde cero).
- Almacén de borradores: `save_draft()`/`load_draft()` en
  `<config_dir>/<padre>/drafts/<draft_id>.json` (`draft_id` 12 hex) con `owner`,
  `created_at` y TTL (`DRAFT_TTL_S`, 1 h). `load_draft` aplica dueño + TTL:
  400 id inválido, 403 de otro usuario, 404 ausente/expirado.
- Guard `smac_api._guard_new_simulation()`: reutiliza `validate_for_backend`
  (whitelist + valores estructurales + compatibilidad de modelo + límites
  recalculados con datos medidos) y `_validate_new_simulation` (padre + draft).
  Falla → **400** + auditoría `action_rejected` con `guard: cost-model`.
- `smac_chat.execute_new_simulation()` (nueva): guarda el borrador y devuelve
  `{ok, draft_id, parent, redirect, detail}`; `redirect` = `?draft=<id>`.
- **Anti open-redirect**: `is_allowed_navigate_url()` acepta la URL exacta **o**
  el patrón estricto `^/platform/dashboard/deployment/\?draft=[0-9a-f]{12}$`
  (`DRAFT_URL_PATTERN`). `_llm_navigate_actions` lo usa; se conserva el test de
  URL maliciosa y se añade uno por patrón.
- **Semántica de pausa** (`handle_confirm`): aceptar `propose_new_simulation`
  **no** limpia el marcador de auto-pausa (la original sigue pausada; se audita
  `auto_resume_skipped` con `reason: "new simulation draft accepted"`,
  `draft_id`, `parent`) y la respuesta trae `auto_resumed: False`. Rechazar
  reanuda. `_execute_record` audita `action_executed` con `draft_id`, `parent`,
  `changes` y costo proyectado.
- `app.py`: nueva ruta `GET /platform/smac/draft/{draft_id}` (auth + dueño +
  TTL; 400/403/404 correctos).
- Tests: `tests/test_new_simulation_draft.py` (whitelist y cambios, topología
  suelta nodos + hint + attrs perdidos, `resume_from` explícito, patrón de URL,
  guard rechaza sin borrador, aceptar crea borrador y mantiene pausada, dueño/
  TTL/id inválido, rechazar reanuda y viewer 403).
