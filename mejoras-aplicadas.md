# Mejoras aplicadas — Sistema Multi-Agente Cognitivo (SMAC) sobre NEBULA

**Fecha:** 28 de septiembre de 2026
**Documento de diseño:** `plan-mas-cognitivo-nebula.md`
**Objetivo:** formalizar el módulo `situationalawareness` de NEBULA como un verdadero sistema
multi-agente (con creencias persistentes, coordinación inter-nodo y aprendizaje) y añadir una
capa cognitiva opcional basada en LLM servido por Groq, sin romper compatibilidad con escenarios
ni configuraciones existentes.

---

## 1. Resumen ejecutivo

| Fase del plan | Estado | Qué se hizo |
|---|---|---|
| Fase 1 — Formalización del framework MAS | ✅ Implementada | `CognitiveAgent`, extensión de `SACommand`, nueva política de arbitraje, `SAReasoner` tolerante + hook cognitivo |
| Fase 2 — Agentes predictivos deterministas | ✅ Implementada | `StragglerPredictor`, `AggregationSelector`, `ResourceBroker` |
| Fase 3 — Políticas aprendidas (bandits) | ⏳ Futura | Arquitectura preparada (`CognitiveAgent` + creencias); predictor usa EWMA + bandas estadísticas |
| Fase 4 — Integración con Groq | ✅ Implementada | `GroqReasoningAgent` asesor, salida estructurada, timeout, degradación segura |
| Fase 5 — Coordinación inter-nodo | ✅ Implementada | `NegotiationMessage` (protobuf), `NegotiationManager`, consentimiento mutuo opt-in en `SANetwork` |
| Fase 6 — Observabilidad y evaluación | ✅ Parcial | `CognitiveDecisionLog` (memoria + JSONL descargable); métricas comparativas pendientes |

**Principio de seguridad respetado en todo el desarrollo:** la capa cognitiva/LLM es asesora y
periódica, nunca está en el camino crítico de la ronda. Ante cualquier fallo o timeout, el
sistema sigue funcionando solo con los agentes deterministas.

---

## 2. Fase 1 — Formalización del framework MAS

### 2.1 `CognitiveAgent` (nuevo)

**Archivos:**

- `nebula/core/situationalawareness/awareness/cognitive/__init__.py` (nuevo)
- `nebula/core/situationalawareness/awareness/cognitive/cognitiveagent.py` (nuevo)

Formaliza el contrato `SAMComponent` como un agente con ciclo explícito
**percepción → creencia → decisión**:

- `perceive()` — observaciones locales sin efectos secundarios.
- `update_belief() / get_belief() / get_beliefs() / merge_observations()` — estado de creencias
  persistente entre rondas (protegido con lock asíncrono).
- `snapshot()` — resumen anonimizado del agente para la capa cognitiva (solo metadatos
  agregados; nunca datos de entrenamiento, gradientes ni pesos).
- `sa_component_actions()` — punto de entrada del ciclo, a implementar por cada subclase.

### 2.2 Extensión de `SACommand` (modificado)

**Archivo:** `nebula/core/situationalawareness/awareness/sautils/sacommand.py`

- Nuevos campos en `SACommand`: `confidence` (float en [0, 1], `1.0` para agentes
  deterministas), `explanation` (texto auditable) y `ttl_rounds` (caducidad en rondas).
- Nuevos métodos: `get_confidence()`, `get_explanation()`, `get_ttl_rounds()`,
  `set_issued_round()`, `is_expired(current_round)`, `get_command_type()`, `to_dict()`
  (serialización para prompts, logs y dashboard) y `from_llm_decision()` (mapea el JSON
  estructurado del LLM a `SACommand`; `no_action` → `None`).
- Nuevos tipos `SACommandType.TRAINING` y `SACommandType.COGNITIVE`.
- Nuevas acciones: `ADJUST_TRAINING_POLICY`, `REQUEST_TOPOLOGY_CHANGE`, `SELECT_AGGREGATOR`,
  `FLAG_FOR_REVIEW`, `PAUSE_LOCAL_TRAINING`, `REDUCE_BATCH_SIZE`, `EXCLUDE_NODE`.
- Nuevas subclases `TrainingCommand` y `CognitiveCommand` (esta última con prioridad `LOW`
  y TTL de 3 rondas por defecto: asesora, no autoridad), registradas en `factory_sa_command`
  como `"training"` y `"cognitive"`.
- `ConnectivityCommand` y `AggregationCommand` aceptan `confidence/explanation/ttl` como
  argumentos solo-nominales (sin romper ninguna llamada posicional existente).

### 2.3 Política de arbitraje con desempate cognitivo (nuevo + modificado)

**Archivos:**

- `nebula/core/situationalawareness/awareness/arbitrationpolicies/priorityllmtiebreak.py` (nuevo)
- `nebula/core/situationalawareness/awareness/arbitrationpolicies/arbitrationpolicy.py` (modificado)
- `nebula/core/situationalawareness/awareness/arbitrationpolicies/staticarbitrationpolicy.py` (modificado)

- Nueva política `PriorityLLMTiebreakPolicy`, seleccionable como `priority_with_llm_tiebreak`
  (también añadida al desplegable del frontend, ver §6). Hereda la regla estática y añade
  `is_ambiguous_tie()`: detecta empates irresolubles (misma prioridad **y** mismo peso
  estático) para pedir una "segunda opinión" al LLM.
- Pesos estáticos extendidos sin alterar los existentes (`SATraining: 1`, `SANetwork: 2`,
  `SAReputation: 3`): `StragglerPredictor: 2`, `AggregationSelector: 3`,
  `ResourceBroker: 1` y `GroqReasoning: 0` (el LLM es asesor: en el fallback estático
  siempre pierde contra telemetría directa).

### 2.4 `SAReasoner` robustecido (modificado)

**Archivo:** `nebula/core/situationalawareness/awareness/sareasoner.py`

- **Carga tolerante de componentes:** secciones de configuración ausentes generan aviso y se
  omiten (compatibilidad con escenarios creados antes de SMAC); soporte de `class_name`
  explícito para agentes multi-palabra.
- **Seguimiento de ronda** (`current_round`) a partir de `RoundEndEvent`.
- **Hook de desempate cognitivo** (`_resolve_tie_with_cognition`): ante empate ambiguo,
  consulta al `GroqReasoningAgent` con timeout acotado; si falla o se abstiene, aplica la
  regla estática. La ronda nunca se bloquea por el LLM.
- **Filtrado de comandos expirados** (TTL) antes del arbitraje.
- **Registro de decisiones**: cada comando seleccionado se anota en el `CognitiveDecisionLog`
  (ver §7) y se le sella la ronda de emisión.
- Expone `get_component()`, `current_round`, `decision_log` y `negotiation`.
- `stop()` ahora también detiene el `NegotiationManager`.

---

## 3. Fase 2 — Agentes predictivos deterministas

### 3.1 `StragglerPredictor` (nuevo)

**Archivos:** `nebula/core/situationalawareness/awareness/stragglerpredictor/__init__.py`,
`stragglerpredictor.py` (+ `StragglerPredictorAgent` como wrapper `SAModuleAgent`).

- Observa `RoundStartEvent` (inicio de ronda), `UpdateReceivedEvent` (latencia de llegada por
  vecino) y `RoundEndEvent` (duración base de ronda).
- Mantiene ventanas deslizantes configurables (`history_window: 20`) y predice con
  `media + std_factor·σ > deadline` (`deadline = max(mediana·2, 10s)`).
- Emite comandos `EXCLUDE_NODE` (prioridad `HIGH`, TTL 2 rondas) con confianza proporcional
  al exceso sobre el deadline. Función pura `predict_stragglers()` testeable.
- Sin baseline suficiente o con pocas muestras (`min_samples: 3`), no predice (conservador).

### 3.2 `AggregationSelector` (nuevo)

**Archivos:** `nebula/core/situationalawareness/awareness/aggregationselector/__init__.py`,
`aggregationselector.py` (+ `AggregationSelectorAgent`).

- Reacciona a `AggregationEvent` y evalúa el contexto: updates recibidos vs. ausentes, nodos
  en blacklist y coeficiente de variación de los pesos.
- Reglas deterministas (`choose_aggregation()`): sospecha bizantina con ≥4 updates → `Krum`
  (si hay menos, `Median`); muchas ausencias o alta dispersión → `TrimmedMean`; dispersión
  moderada → `Median`; contexto estable → `FedAvg`.
- El comando `SELECT_AGGREGATOR` **aplica la selección con efecto real** (hot-swap del
  agregador vía `create_aggregator` + `init()`, actualizando
  `aggregator_args.algorithm`) y **devuelve los updates intactos**, por lo que es seguro en
  cualquier posición del arbitraje. Sin acceso al engine (tests), la elección queda como
  creencia asesora.
- Solo emite comando cuando la elección cambia (evita ruido en el arbitraje).

### 3.3 `ResourceBroker` (nuevo)

**Archivos:** `nebula/core/situationalawareness/awareness/resourcebroker/__init__.py`,
`resourcebroker.py` (+ `ResourceBrokerAgent`).

- Sondea `SystemMonitor` en cada fin de ronda con **sondas rápidas** (se evita a propósito el
  probe de red de 5 s para no retrasar el arbitraje) + GPU vía NVML si existe.
- Presión crítica (≥95 % en CPU/RAM/GPU) → `PAUSE_LOCAL_TRAINING` (`HIGH`); uso alto
  (≥85 % CPU, ≥90 % RAM/GPU, configurables) → `REDUCE_BATCH_SIZE` (`MEDIUM`, factor 0.5).
- Cooldown en rondas (`cooldown_rounds: 3`) contra oscilaciones. Función pura
  `decide_resource_action()` testeable.

---

## 4. Fase 4 — Capa cognitiva con Groq

**Archivos:** `nebula/core/situationalawareness/awareness/cognitive/groqreasoningagent.py`,
`nebula/core/situationalawareness/awareness/groqreasoningagent/__init__.py`,
`nebula/core/situationalawareness/awareness/groqreasoningagent/groqreasoningagent.py` (shim
para el cargador dinámico).

- `GroqReasoning(CognitiveAgent)` + wrapper `GroqReasoningAgent` (`GroqReasoning_MainAgent`,
  peso de arbitraje 0).
- Llamadas con `httpx.AsyncClient`, `POST https://api.groq.com/openai/v1/chat/completions`,
  `response_format` con esquema JSON estricto (`DECISION_SCHEMA`: `decision`, `target_agent`,
  `parameters`, `confidence`, `explanation`), validación de la respuesta en código.
- Modelo por defecto `openai/gpt-oss-20b` con `reasoning_effort=low` (configurable; el nombre
  del modelo **nunca** está hardcodeado).
- Dos modos de invocación: **periódico** (cada `invoke_every_n_rounds: 5` rondas) y
  **bajo demanda** como resolutor de empates (`resolve_tie()` mapea
  `parameters.preferred_action` al comando ganador; abstención → fallback estático).
- **Privacidad:** solo metadatos agregados y anonimizados (direcciones IP hasheadas con
  SHA-256 truncado, función `anonymize_addr`). Nunca pesos, gradientes ni datos.
- **Degradación segura:** timeout agresivo (0.3 s), cualquier error de transporte/parseo/
  validación → sin sugerencia; sin API key → agente autodesactivado con aviso. La key se lee
  de `GROQ_API_KEY` (variable configurable vía `api_key_env_var`), **nunca** del JSON del
  escenario.
- Dependencia `httpx==0.28.1` añadida al grupo `core` de `pyproject.toml` + `uv.lock`
  (diff mínimo de 4 líneas; ya estaba presente como transitiva).
- **SDK oficial de Groq** (`groq==1.7.0` en grupos `core` y `frontend`): todas las llamadas
  pasan por `nebula/core/smac_groq.py` (`AsyncGroq`, `temperature=1`, `top_p=1`,
  `max_completion_tokens=2048`, `reasoning_effort` configurable, `stream=False`, salidas
  estructuradas), con fallback transparente a `httpx` en imágenes sin el SDK. El chat
  usa por defecto `openai/gpt-oss-120b` + effort `medium` (envs `SMAC_CHAT_MODEL` /
  `SMAC_CHAT_REASONING_EFFORT`); los agentes de nodo mantienen `gpt-oss-20b` + `low`
  por latencia (cambiable por escenario).

---

## 5. Fase 5 — Coordinación inter-nodo (contract-net)

### 5.1 Nuevo mensaje protobuf (modificado + regenerado)

**Archivos:** `nebula/core/pb/nebula.proto`, `nebula/core/pb/nebula_pb2.py` (regenerado),
`nebula/core/network/actions.py`, `nebula/core/network/messages.py`.

- `NegotiationMessage { PROPOSE | ACCEPT | REJECT | COUNTER_PROPOSAL, proposal_id, edge_a,
  edge_b, change (connect|disconnect), reason, ttl_rounds, round }` + campo 16 en el
  `Wrapper` oneof.
- `nebula_pb2.py` regenerado con `grpcio-tools==1.60.0` (protoc de la era protobuf 4.x,
  compatible con el runtime `protobuf==4.25.3` del proyecto; round-trip verificado).
- `NegotiationAction` registrado en `ACTION_CLASSES` y plantilla `"negotiation"` en
  `MessagesManager` (creación/parseo verificados).

### 5.2 `NegotiationManager` (nuevo) + consentimiento mutuo

**Archivos:** `nebula/core/situationalawareness/awareness/negotiation/__init__.py`,
`negotiationmanager.py`; modificado `sanetwork.py` (métodos + rama `DISCONNECT`).

- Protocolo: A propone cambio de arista a B → B evalúa con su `SAReasoner` local (sin
  reestructuración en curso + sin propuesta conflictiva pendiente) y responde
  `ACCEPT/REJECT` (un `COUNTER_PROPOSAL` se evalúa como propuesta con el mismo id).
- Propuestas con UUID, TTL en rondas (`timeout_rounds: 2`, expiración en `RoundEndEvent`) y
  espera acotada (`wait_timeout_s: 5.0`).
- `SANetwork` propone al sugerir `DISCONNECT` y envuelve la ejecución:
  **solo desconecta con ACCEPT mutuo**; con REJECT/timeout omite la acción (la propuesta
  caduca y puede reintentarse). Comando `parallelizable=True` para no bloquear el resto.
- **Opt-in y seguro:** `sa_reasoner.negotiation.enabled` por defecto `false`, de modo que
  federaciones mixtas (pares sin SMAC que nunca responderían) conservan el comportamiento
  unilateral clásico.

---

## 6. Configuración y frontend (modificados)

**Archivos:** `nebula/controller/scenarios.py`,
`nebula/frontend/templates/deployment.html`.

- `scenarios.py` genera las nuevas secciones con defaults del plan: `straggler_predictor`
  (`history_window: 20`, `min_samples: 3`, `std_factor: 2.0`), `aggregation_selector`
  (`missing_ratio_threshold: 0.3`, `variance_threshold: 1.0`), `resource_broker`
  (`cpu_high: 85.0`, `mem_high: 90.0`, `gpu_mem_high: 90.0`, `cooldown_rounds: 3`),
  `groq_reasoning_agent` (`model: openai/gpt-oss-20b`, `reasoning_effort: low`,
  `timeout_s: 0.3`, `invoke_every_n_rounds: 5`, `invoke_on_tiebreak: true`) y
  `negotiation` (desactivada por defecto).
- El desplegable *Arbitration Policy* del dashboard ofrece ahora `priority_with_llm_tiebreak`
  además de `sap`.

---

## 7. Observabilidad (Fase 6, parcial)

**Archivo:** `nebula/core/situationalawareness/awareness/cognitive/decisionlog.py`

- `CognitiveDecisionLog`: buffer circular en memoria (500 registros) + persistencia JSONL en
  `app/logs/<escenario>/smac_decisions.jsonl` (timestamp, ronda, owner, acción, tipo,
  prioridad, confianza, explicación). Al vivir en la carpeta de logs del escenario, se
  incluye automáticamente en el zip de *"Download logs"* del dashboard.
- Métricas comparativas de evaluación (tabla del §8 del plan) pendientes de ejecución sobre
  escenarios con churn/movilidad/stragglers inyectados.

---

## 8. Compatibilidad y despliegue

- **Backward compatible:** escenarios y JSONs anteriores (sin secciones SMAC) cargan igual;
  los componentes nuevos se omiten con un aviso. Ninguna firma existente cambió de forma
  incompatible (solo añadidos keyword-only con defaults).
- **Sin reconstruir imágenes:** frontend, controller y nodos montan el repo en vivo
  (`<root>:/nebula`), así que basta con **reiniciar la plataforma**
  (`python app/main.py --stop` + `python app/main.py`) para activar el código. Los
  escenarios antiguos deben **recrearse** para obtener las nuevas secciones de config.
- Pesos del arbitraje existentes intactos; el LLM no puede imponerse a la telemetría.

---

## 9. Verificación realizada

- Suite scratch `/tmp/test_smac.py`: **51/51 checks** (extensiones de `SACommand`,
  `from_llm_decision` + casos borde, factorías y fallback, empates ambiguos, lógica pura de
  los 3 agentes, decision log en memoria y fichero, Groq con transporte mockeado —
  éxito/timeout/sin-key/payload inválido/resolve_tie —, manager de negociación y round-trip
  protobuf). Detectó y se corrigió un bug real (rama `Median` inalcanzable en el selector).
- `ruff check` limpio en todos los ficheros nuevos; `ruff format` aplicado.
- Imports de los 19 módulos tocados + resolución del cargador dinámico para los 6
  componentes (`sa_network`, `sa_training` y los 4 nuevos) verificados.

---

## 10. Trabajo futuro

1. **Fase 3:** envolver neighbor-policies/training-policies con bandits contextuales usando
   las heurísticas actuales como fallback (base lista: creencias + `snapshot()`).
2. **Fase 6 completa:** panel de decisiones/explicaciones en el frontend y batería de
   escenarios comparativos con las métricas del plan.
3. Evaluación del devilvery: `COUNTER_PROPOSAL` activa (generar contraofertas, hoy solo se
   evalúan las recibidas) y consulta al LLM local en propuestas ambiguas.
4. Activar `negotiation.enabled: true` por defecto cuando toda la federación ejecute SMAC.
5. Revisar `https://console.groq.com/docs/models` antes de cada despliegue (el catálogo de
   modelos cambia con frecuencia) y ajustar `model` en la configuración si procede.

---

## 11. Chatbot conversacional SMAC Chat (nuevo)

**Archivos nuevos:** `nebula/frontend/smac_chat.py` (backend),
`nebula/frontend/smac_api.py` (orquestación §12),
`nebula/frontend/templates/smac_chat.html`, `nebula/frontend/static/js/smac-chat.js`,
`nebula/core/operatorbus.py`.
**Modificados:** `nebula/frontend/app.py` (página + 8 rutas API), `nebula/frontend/templates/dashboard.html`
(botones), `nebula/core/engine.py` (hooks mínimos), `app/deployer.py` +
`nebula/controller/scenarios.py` (passthrough de `GROQ_API_KEY`), `pyproject.toml` +
`uv.lock` (`httpx` al grupo `frontend`, `groq==1.7.0` a `core` y `frontend`).

El sistema multi-agente se opera desde el dashboard mediante el **panel cognitivo SMAC**
(botón *SMAC Chat* o icono de chat por escenario, con el escenario precargado). No es un
chat de texto lineal, sino un **panel de observabilidad cognitiva y control interactivo**
dividido espacialmente: cabecera de estado (ronda, agregador, conteos), **topología viva
en SVG** correlacionada con el razonamiento (click en un nodo para inspeccionarlo y
resaltar sus decisiones), **feed de razonamiento** (tarjetas de decisión con confianza y
explicación + análisis LLM bajo demanda vía `/platform/smac/analyze`), **controles**
(Pause/Resume, cambio de agregador, propuesta de desconexión, stop — propuestos vía
`/platform/smac/propose` y ejecutados solo tras *Confirm & run*) y una entrada compacta
de lenguaje natural como canal secundario. El panel sondea `/platform/smac/state`
(escenario + topología + nodos + decisiones) cada 5 s.

- **Consultar:** el backend construye un snapshot vivo del escenario (config, filas de
  nodos, estado de contenedores Docker, últimas decisiones SMAC con explicaciones) y el
  LLM responde fundamentado en él (rondas, stragglers, topología, porqués).
- **Actuar (siempre con confirmación):** el LLM propone acciones estructuradas y la UI
  muestra *Confirm & run / Discard*. Acciones v1: `pause_node`/`resume_node` (Docker
  pause/unpause: congelación instantánea sin pérdida de estado), `select_aggregator`
  (hot-swap vía bus de operador), `propose_disconnect` (negociación inter-nodo vía bus),
  `stop_scenario` (API del controller).
- **Bus de operador:** ficheros en `app/config/<escenario>/operator_commands/` que cada
  nodo consume con `OperatorBus` (poll cada 5 s, resultados por nodo, TTL y anti-doble
  ejecución); el engine soporta `pause` (bandera `operator_paused` chequeada en cada
  ronda) y arranque/parada del bus en `deploy_components`/`_shutdown_protocol`.
- **Seguridad:** rutas con sesión requerida, acciones pendientes de un solo uso con TTL de
  10 min, `GROQ_API_KEY` reenviada automáticamente a frontend/controller/nodos (sin ella
  el chat se reporta como desactivado).

**Verificación:** suite `/tmp/test_smac_chat.py` **27/27** (contexto, Groq mockeado,
pendientes/confirmación, round-trip del bus con pausa/broadcast/algoritmo/desconexión y
ejecutores tolerantes); `ruff check` limpio y `ruff format` aplicado; suite SMAC previa
sigue en **51/51**.
- **Fix verificado en producción:** Groq rechaza `response_format` con `strict: true` cuando
  el esquema tiene objetos libres (`params`/`parameters`) — error 400. Ambos esquemas usan
  `strict: false` y la estructura se valida en código. Además los fallos de Groq ahora se
  loguean como warning (sin exponer la key) en vez de debug silencioso.
- **Fix verificado en producción:** con contexto grande + reasoning `medium`, el JSON salía
  truncado por `max_completion_tokens=2048`. Chat usa 4096 (env `SMAC_CHAT_MAX_TOKENS`) y
  `extract_json_object()` recupera JSON con fences/prosa; lo irrecuperable degrada con
  warning.

---

## 12. Spec SMAC-CHAT-SPEC.md (backend aplicable al panel)

**Nuevo:** `nebula/frontend/smac_api.py` (orquestación testeable) + 3 rutas
(`/platform/api/smac/actions/execute`, `/events` SSE, `/metrics`).
**Implementado:** `request_id` con eco + serialización por escenario (lock), degradación
determinista ES/EN (config vs runtime, siguiente paso), política de idioma en prompts,
split `scenario_config`/`runtime` en el snapshot, `sources` auditables, RBAC
(admin/operator/viewer) con `stop_scenario` solo-admin y confirmación reforzada
(escribir el escenario), tokens de un solo uso con TTL, auditoría `smac_audit.jsonl`,
rate-limit 20/min, métricas (contadores + latencia p50/p95), caché de snapshot 45 s,
alertas proactivas SSE (`predicted_straggler`, `node_offline`, `scenario_finished`…).
Panel JS: `request_id`, línea "Based on", input de confirmación para stop, EventSource.
**No implementado (diseño futuro):** dock flotante F0, streaming token-a-token,
bucle tool-calling multi-vuelta, historial en servidor, i18n de la UI, voz.
**Verificación:** `/tmp/test_smac_api.py` **34/34** + E2E en vivo (ask con Groq real en
español + sources, propose→execute-token, SSE, metrics); la spec marca estados honestos.

---

## 13. UX del panel y fixes de plataforma

- **Eco de tus mensajes + texto estructurado** (`smac-chat.js`, `smac_chat.html`): el feed
  muestra tus mensajes como tarjetas "🧑 operator" y las respuestas "🤖 smac" con render
  estructurado propio sin dependencias — `**negrita**`, secciones `##`, listas `-`/`1.`,
  `código` y párrafos — aplicado también a análisis y explicaciones de decisiones. El HTML
  se escapa antes de formatear (verificado anti-XSS con prueba en Node).
- **Fix del puerto 6060** (`nebula/utils.py`): `SocketUtils.is_port_open()` hacía `bind()`
  sin `SO_REUSEADDR` y las conexiones en `TIME_WAIT` (p. ej. de health-checks previos a
  través del proxy Docker) lo engañaban, desviando el frontend al 6061. Con el flag, un
  puerto realmente libre se detecta bien y uno ocupado de verdad sigue detectándose.
- **Correlación decisión↔nodo** (`sareasoner.py`): el decision log guarda el `target` de
  cada comando para que el panel resalte las decisiones del nodo seleccionado.
- **Fix online/offline** (`smac_chat.py`): la query de nodos no traía `timestamp` y ningún
  nodo salía online; añadida la columna (detectado por test).
