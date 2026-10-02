# Sistema Multi-Agente Adaptativo y Cognitivo para Aprendizaje Federado Dinámico sobre NEBULA

**Integración propuesta con Groq (inferencia LPU de baja latencia) como motor de razonamiento cognitivo**

---

## 0. Resumen ejecutivo

NEBULA ya incluye un módulo llamado `situationalawareness` que funciona, en la práctica, como un **framework mono-agente-por-nodo**: cada nodo instancia un `SAReasoner` que carga componentes (`SAMComponent`), recoge sus sugerencias en un blackboard (`SuggestionBuffer`) y las arbitra por prioridad. Esto es una base excelente, pero es **reactivo y basado en heurísticas fijas** (políticas de vecinos y de entrenamiento predefinidas), no **cognitivo** ni **predictivo**, y no coordina agentes *entre* nodos, solo *dentro* de un nodo.

Este documento amplía la propuesta original y añade un componente que el enunciado inicial no contemplaba: el uso de un **modelo de lenguaje servido por Groq** (inferencia ultra-rápida sobre LPU) como **capa cognitiva de razonamiento de alto nivel**, separada de la capa de control de bajo nivel (que sigue siendo determinista, local y sin dependencia de red externa por razones de robustez y latencia).

El documento cubre:

1. Diagnóstico técnico de la arquitectura actual de NEBULA (con referencias exactas a archivos).
2. Arquitectura objetivo del Sistema Multi-Agente Cognitivo (SMAC).
3. Catálogo completo de agentes, sus entradas/salidas y su ubicación en el código.
4. Diseño detallado de la integración con Groq: qué modelo usar, por qué, cómo, con qué esquema de prompts/salidas estructuradas, y cómo mitigar los riesgos de usar un servicio externo dentro de un sistema de tiempo real.
5. Protocolo de coordinación inter-nodo (negociación, no solo arbitraje local).
6. Cambios de configuración necesarios (JSON) y nuevas interfaces de clases (pseudocódigo Python fiel al estilo del proyecto).
7. Plan de evaluación, métricas, cronograma por fases y riesgos.

---

## 1. Diagnóstico de la arquitectura actual de NEBULA

| Pieza existente | Ubicación en el repo | Rol actual | Limitación frente al objetivo "cognitivo" |
|---|---|---|---|
| `SAReasoner` | `nebula/core/situationalawareness/awareness/sareasoner.py` | Orquesta componentes SA, arbitra sugerencias por prioridad | El arbitraje es solo **intra-nodo**; la resolución de conflictos es un algoritmo de prioridades, no negociación |
| `SAMComponent` (interfaz) | mismo archivo | Contrato base para "agentes" (`init`, `sa_component_actions`) | No define ciclo percepción→creencia→decisión; no hay estado persistente de "creencias" |
| `SANetwork` | `.../sanetwork/sanetwork.py` + `neighborpolicies/*.py` (ring, star, fc, distance, idle) | Decide reestructuración de topología | Reglas estáticas por tipo de topología, no aprendidas ni anticipatorias |
| `SATraining` | `.../satraining/satraining.py` + `trainingpolicy/*.py` (qds, hts, bps, fastreboot) | Adapta la estrategia de entrenamiento | Reacciona a rondas ya completadas, no predice stragglers antes de que ocurran |
| `SuggestionBuffer` | `.../suggestionbuffer.py` | Blackboard de sugerencias por evento | Vale como bus de mensajes intra-nodo; falta el equivalente inter-nodo |
| `SACommand` | `.../sautils/sacommand.py` | Comando ejecutable con prioridad y `conflicts_with()` | Buen punto de extensión: se puede añadir `confidence`, `explanation`, `ttl` |
| `SystemMonitor` | `.../sautils/sasystemmonitor.py` | CPU/GPU/RAM/red por nodo (psutil, pynvml) | Es la fuente de telemetría; falta series temporales e histórico por vecino |
| `EventManager` / `nebulaevents.py` | `nebula/core/eventmanager.py`, `nebula/core/nebulaevents.py` | Bus pub/sub: `RoundStartEvent`, `RoundEndEvent`, `AggregationEvent`, `UpdateNeighborEvent`, `NodeBlacklistedEvent`, `NodeFoundEvent`, `ModelPropagationEvent`, `UpdateReceivedEvent`, etc. | Ya es el "sistema nervioso" ideal para disparar agentes; se reutiliza sin cambios |
| `CandidateSelector` (std/dist/ring/fc) | `.../discovery/candidateselection/*.py` | Selecciona candidatos de conexión al descubrir nodos | Reglas fijas por topología, sin aprendizaje |
| `FederationConnector` | `.../discovery/federationconnector.py` | Descubrimiento y conexión a la federación | Punto natural para inyectar negociación inter-nodo |
| `CommunicationsManager` / `propagator.py` | `nebula/core/network/*.py` | Transporte de mensajes y modelos entre nodos | Se reutiliza como canal para el nuevo protocolo de negociación de agentes |
| `Aggregator` (`fedavg`, `krum`, `median`, `trimmedmean`) | `nebula/core/aggregation/*.py` | Estrategias de agregación robusta | No hay selección *dinámica* de algoritmo de agregación según contexto (esto se propone como nuevo agente) |

**Conclusión del diagnóstico:** no hay que construir un sistema multi-agente desde cero; hay que **formalizar** el que existe como un verdadero MAS (con estado de creencias, comunicación inter-agente e inter-nodo, y aprendizaje), y **añadir una capa cognitiva** que hoy no existe en absoluto.

---

## 2. Objetivos del Sistema Multi-Agente Cognitivo (SMAC)

1. **Anticipar**, no solo reaccionar a: nodos rezagados (stragglers), caídas de enlace, desconexiones por movilidad, degradación de batería/CPU.
2. **Aprender** políticas de topología y de entrenamiento en lugar de aplicar reglas fijas, sin perder un modo de operación seguro por heurística (fallback).
3. **Coordinar** decisiones entre nodos vecinos (no solo dentro de un nodo) mediante un protocolo de negociación ligero.
4. **Explicar** sus decisiones (auditable, importante en entornos de edge computing multi-stakeholder) usando un LLM rápido como capa de razonamiento en lenguaje natural sobre el estado ya cuantificado por los agentes deterministas.
5. Mantener **latencia y coste acotados**: la capa cognitiva basada en LLM es *asesora y periódica*, nunca un cuello de botella en el camino crítico de cada ronda de entrenamiento.

---

## 3. Arquitectura objetivo

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CAPA COGNITIVA (nueva, opcional)                │
│  GroqReasoningAgent  →  LLM en Groq (gpt-oss-120b / kimi-k2 / ...)   │
│  - Resume telemetría agregada de todos los agentes del nodo          │
│  - Resuelve conflictos ambiguos que el arbitraje por prioridad no    │
│    puede resolver bien (empates, contextos nuevos no vistos)         │
│  - Genera explicaciones en lenguaje natural para el dashboard        │
│  - Sugiere ajustes de hiperparámetros de las políticas de aprendizaje│
│  Se comunica con el resto SOLO a través de SACommand (mismo contrato)│
└───────────────────────────────▲───────────────────────────────────---┘
                                 │ (asíncrono, no bloqueante, con timeout)
┌───────────────────────────────┴───────────────────────────────────---┐
│                CAPA DE AGENTES REACTIVOS/APRENDICES (existente        │
│                        + extendida)                                  │
│  SAReasoner (por nodo) ─┬─ SANetwork (topología)                     │
│                         ├─ SATraining (estrategia de entrenamiento)   │
│                         ├─ StragglerPredictorAgent   (NUEVO)          │
│                         ├─ AggregationSelectorAgent  (NUEVO)          │
│                         └─ ResourceBrokerAgent       (NUEVO)          │
│  Arbitraje intra-nodo: SuggestionBuffer + ArbitrationPolicy (existe)  │
└───────────────────────────────▲───────────────────────────────────---┘
                                 │ eventos (EventManager / nebulaevents.py)
┌───────────────────────────────┴───────────────────────────────────---┐
│                 CAPA DE PERCEPCIÓN / TELEMETRÍA (existente)           │
│  SystemMonitor (CPU/GPU/RAM/red) · RoundEndEvent · AggregationEvent   │
│  UpdateNeighborEvent · NodeBlacklistedEvent · NodeFoundEvent          │
└───────────────────────────────▲───────────────────────────────────---┘
                                 │
┌───────────────────────────────┴───────────────────────────────────---┐
│         CAPA DE COORDINACIÓN INTER-NODO (nueva)                      │
│  Protocolo de negociación tipo contract-net sobre                    │
│  CommunicationsManager / propagator.py                               │
│  (evita que dos nodos reestructuren la misma arista en conflicto)    │
└───────────────────────────────────────────────────────────────────---┘
```

Principio de diseño clave: **la capa cognitiva (Groq) nunca decide sola ni en el camino crítico**. Emite *sugerencias* con un `confidence` y una `explanation`, empaquetadas como `SACommand`, y entra al mismo pipeline de arbitraje que ya existe. Si Groq no responde a tiempo (timeout configurable, p. ej. 300 ms) o la API falla, el sistema sigue funcionando solo con los agentes deterministas — **no hay dependencia dura de un servicio externo para que la ronda de FL progrese**.

---

## 4. Catálogo de agentes

| Agente | Tipo | Frecuencia | Entradas | Salida (`SACommand`) | Nuevo/Existente |
|---|---|---|---|---|---|
| `SANetwork` | Reactivo con política enchufable | Por `UpdateNeighborEvent`/`NodeFoundEvent` | Vecinos conocidos, topología objetivo | Reestructurar conexión | Existente, se envuelve con política aprendida (fase 3) |
| `SATraining` | Reactivo con política enchufable | Por `RoundEndEvent` | Métricas de ronda, vecinos | Ajustar hiperparámetros / nº de épocas locales | Existente, ídem |
| `StragglerPredictorAgent` | **Predictivo** (regresión online / bandit contextual) | Por `RoundStartEvent`, continuo | Histórico de latencia por vecino + `SystemMonitor` | Excluir/retrasar nodo, redistribuir carga | **Nuevo** |
| `AggregationSelectorAgent` | Basado en reglas + aprendizaje | Por `AggregationEvent` | Nº de stragglers, sospecha de nodos maliciosos (blacklist), varianza de updates | Elegir `fedavg`/`krum`/`median`/`trimmedmean` dinámicamente | **Nuevo** |
| `ResourceBrokerAgent` | Reactivo | Continuo (poll a `SystemMonitor`) | CPU/GPU/batería/ancho de banda | Sugerir pausa de entrenamiento local, bajar tamaño de batch | **Nuevo** |
| `GroqReasoningAgent` | **Cognitivo (LLM)** | Periódico (p. ej. cada N rondas) o bajo demanda en empates de arbitraje | Resumen JSON agregado de todos los agentes anteriores | Explicación en lenguaje natural + sugerencia de alto nivel (mapeada a `SACommand`) | **Nuevo** |
| Protocolo de negociación inter-nodo | Coordinación | Al proponerse cambio de topología | Propuesta de `SANetwork` local + de vecinos | Acuerdo/rechazo de cambio de arista | **Nuevo** |

---

## 5. Integración con Groq — diseño detallado

### 5.1 Por qué Groq y qué modelo usar

Groq no entrena modelos propios: sirve modelos de pesos abiertos (Llama, GPT-OSS, Kimi, Qwen, etc.) sobre su hardware LPU, cuya ventaja diferencial es **latencia de inferencia muy baja y alto throughput** vía una API compatible con OpenAI (`https://api.groq.com/openai/v1`). Esto es exactamente lo que se necesita para una capa de razonamiento que se invoca con frecuencia (cada pocas rondas, en cada nodo edge) sin convertirse en cuello de botella.

Catálogo de producción de Groq relevante (verificar siempre en `https://console.groq.com/docs/models`, ya que cambia con el tiempo — por ejemplo `llama-3.1-8b-instant` fue retirado a favor de `openai/gpt-oss-20b` en agosto de 2026):

| Modelo Groq | Rol recomendado en el SMAC | Motivo |
|---|---|---|
| `openai/gpt-oss-20b` | Razonamiento por defecto en el `GroqReasoningAgent` de cada nodo edge | Rápido, barato, soporta *reasoning effort* configurable (low/medium/high) y salidas estructuradas (`structuredOutputs`) |
| `openai/gpt-oss-120b` | Razonamiento en nodos "coordinadores" o cuando hay presupuesto de latencia mayor (p. ej. análisis post-experimento, no en el camino de cada ronda) | Mayor capacidad de razonamiento para conflictos complejos de arbitraje |
| `moonshotai/kimi-k2-instruct-0905` | Generación de reportes/explicaciones más largas para el dashboard (análisis post-hoc del experimento completo) | Mejor en tareas agénticas/uso de herramientas y contextos largos (hasta 256k tokens) si se le pasa el histórico completo de eventos |
| `qwen/qwen3-32b` | Alternativa con modo "thinking"/"non-thinking" conmutable, útil para separar decisiones rápidas de análisis más profundos sin cambiar de proveedor de modelo | Flexibilidad reasoning-effort |

Recomendación por defecto para producción en edge: **`openai/gpt-oss-20b`** con `reasoning_effort=low` para el lazo periódico rápido, y `openai/gpt-oss-120b` (o `kimi-k2-instruct-0905`) para el análisis agregado que corre en el nodo controlador/agregador (menos sensible a latencia).

> Nota importante: la lista y precios de modelos de Groq cambian con relativa frecuencia. El código debe leer el nombre del modelo desde configuración (nunca hardcodeado) y el proyecto debe revisar `https://console.groq.com/docs/models` antes de cada despliegue.

### 5.2 Qué NO se envía a Groq (privacidad y seguridad)

- **Nunca** se envían pesos del modelo, gradientes, ni datos de entrenamiento crudos.
- Solo se envían **metadatos agregados y ya anonimizados**: duración de rondas, nº de stragglers, varianza de actualizaciones, estado de topología (grafo de direcciones IP hasheadas o IDs internos, no IPs reales si el despliegue es sensible), y las sugerencias ya emitidas por los agentes deterministas (`SACommand` en su forma serializada).
- Esto es coherente con el principio de FL de no exponer datos de entrenamiento, y reduce además el tamaño del prompt (más barato y más rápido).

### 5.3 Contrato de entrada/salida (salida estructurada)

Se usa **structured outputs** de Groq (`json_schema`, `strictJsonSchema=true`) para forzar que la respuesta del LLM sea parseable de forma determinista a un `SACommand`. Esquema propuesto:

```json
{
  "type": "object",
  "properties": {
    "decision": {
      "type": "string",
      "enum": ["no_action", "adjust_training_policy", "request_topology_change",
               "select_aggregator", "flag_for_human_review"]
    },
    "target_agent": {"type": "string"},
    "parameters": {"type": "object"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "explanation": {"type": "string", "maxLength": 500}
  },
  "required": ["decision", "confidence", "explanation"]
}
```

Este JSON se traduce mecánicamente a un `SACommand` (mismo objeto que ya usan `SANetwork`/`SATraining`), con `owner="GroqReasoningAgent"` y una prioridad configurable (por defecto, **menor** que la de los agentes deterministas basados en telemetría directa — el LLM es asesor, no autoridad final).

### 5.4 Pseudocódigo de integración (fiel al estilo del proyecto)

```python
# nebula/core/situationalawareness/awareness/cognitive/groqreasoningagent.py

import json
import httpx
from nebula.core.situationalawareness.awareness.sareasoner import SAMComponent
from nebula.core.situationalawareness.awareness.sautils.sacommand import SACommand

GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

class GroqReasoningAgent(SAMComponent):
    def __init__(self, config):
        self._config = config
        self._sar = config["sar"]                      # referencia al SAReasoner del nodo
        self._model = config.get("model", "openai/gpt-oss-20b")
        self._reasoning_effort = config.get("reasoning_effort", "low")
        self._timeout_s = config.get("timeout_s", 0.3)   # nunca bloquea el camino crítico
        self._api_key = config["api_key"]                # inyectada vía variable de entorno/secret

    async def init(self):
        self._http = httpx.AsyncClient(timeout=self._timeout_s)

    async def sa_component_actions(self):
        snapshot = await self._build_context_snapshot()   # métricas agregadas y anonimizadas
        try:
            response = await self._http.post(
                GROQ_BASE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "reasoning_effort": self._reasoning_effort,
                    "response_format": {"type": "json_schema", "json_schema": DECISION_SCHEMA},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(snapshot)},
                    ],
                },
            )
            decision = json.loads(response.json()["choices"][0]["message"]["content"])
        except (httpx.TimeoutException, httpx.HTTPError, KeyError):
            # Degradación segura: sin sugerencia del LLM, el sistema sigue con agentes deterministas
            return

        if decision["decision"] != "no_action":
            cmd = SACommand.from_llm_decision(decision, owner=self)
            await self._sar.sb.register_suggestion(cmd)   # mismo pipeline de arbitraje existente
```

Puntos de diseño a resaltar:

- **Timeout agresivo (cientos de ms) y captura explícita de errores**: si Groq no responde, el nodo sigue operando con normalidad.
- **No es una llamada por ronda**: se invoca cada *N* rondas (configurable) o cuando el `ArbitrationPolicy` detecta un **empate** entre dos `SACommand` de igual prioridad que no puede resolver por sí solo (caso de uso natural para pedir una "segunda opinión" cognitiva).
- **Costo acotado**: al usar `gpt-oss-20b` con `reasoning_effort=low` y prompts pequeños (metadatos agregados, no datos crudos), el coste por invocación es mínimo, y Groq está optimizado precisamente para esta clase de latencia.

### 5.5 Dónde encaja en la arquitectura de arbitraje existente

En `SAReasoner._arbitatrion_suggestions()` (ver `sareasoner.py`), cuando dos `SACommand` empatan en prioridad y la `ArbitrationPolicy.tie_break()` no tiene una regla clara, se dispara asíncronamente al `GroqReasoningAgent` como *resolutor de último recurso* antes de descartar ambos comandos. Esto no cambia el contrato de la clase, solo añade una fuente más de sugerencias al `SuggestionBuffer`.

---

## 6. Protocolo de coordinación inter-nodo (nuevo)

Hoy, `SANetwork` decide reestructurar la topología basándose solo en lo que el nodo local observa. Se propone un protocolo ligero tipo *contract-net*:

1. El nodo A detecta (vía `StragglerPredictorAgent` o `SANetwork`) que conviene cambiar una arista con el nodo B.
2. A envía un mensaje `PROPOSE_TOPOLOGY_CHANGE` a B (nuevo tipo de mensaje en `nebula/core/network/messages.py`, transportado por `CommunicationsManager`).
3. B evalúa la propuesta con su propio `SAReasoner` (puede incluir su propio `GroqReasoningAgent` si el cambio es ambiguo) y responde `ACCEPT`/`REJECT`/`COUNTER_PROPOSAL`.
4. Solo si hay `ACCEPT` mutuo se ejecuta el `SACommand` de reestructuración en ambos nodos, evitando estados inconsistentes del grafo.
5. Se define un `NEGOTIATION_TIMEOUT` (p. ej. 2 rondas) tras el cual la propuesta caduca y se puede reintentar.

Esto se apoya en la infraestructura de mensajería ya existente (`propagator.py`, `discoverer.py`, `federationconnector.py`), añadiendo un nuevo tipo de mensaje y un pequeño estado de "propuestas pendientes" por nodo.

---

## 7. Cambios de configuración (JSON)

Extensión de la sección `situational_awareness.sa_reasoner` (usada hoy por `SAReasoner._loading_sa_components`):

```json
{
  "situational_awareness": {
    "strict_topology": false,
    "sa_reasoner": {
      "verbose": true,
      "arbitration_policy": "priority_with_llm_tiebreak",
      "sar_components": {
        "sa_network": true,
        "sa_training": true,
        "straggler_predictor": true,
        "aggregation_selector": true,
        "resource_broker": true,
        "groq_reasoning_agent": true
      },
      "groq_reasoning_agent": {
        "model": "openai/gpt-oss-20b",
        "reasoning_effort": "low",
        "timeout_s": 0.3,
        "invoke_every_n_rounds": 5,
        "invoke_on_tiebreak": true,
        "api_key_env_var": "GROQ_API_KEY"
      },
      "straggler_predictor": {
        "history_window": 20,
        "method": "contextual_bandit"
      }
    }
  }
}
```

La API key de Groq **nunca** se guarda en el JSON de escenario (que puede compartirse/versionarse); se lee de variable de entorno (`GROQ_API_KEY`), siguiendo el mismo patrón que otros secretos del proyecto.

---

## 8. Plan de evaluación

| Métrica | Baseline (FedAvg estático) | Objetivo con SMAC |
|---|---|---|
| Tiempo de convergencia (rondas hasta accuracy objetivo) | referencia | reducción ≥ X% |
| Nº de rondas afectadas por stragglers no anticipados | referencia | reducción significativa (predicción anticipa antes del timeout) |
| Tráfico de red por reestructuraciones de topología | referencia | igual o menor (evitar reestructuraciones contradictorias gracias a negociación) |
| Latencia añadida por la capa cognitiva | — | debe mantenerse muy por debajo de la duración de una ronda de entrenamiento |
| Disponibilidad ante fallo de Groq | — | 100% (degradación segura, sin dependencia dura) |

Escenarios de prueba: usar `nebula/controller/scenarios.py` para simular churn de nodos, movilidad y stragglers inyectados artificialmente (retrasos forzados en `SystemMonitor` simulado), comparando contra los escenarios ya existentes del proyecto.

---

## 9. Cronograma por fases

1. **Fase 1 — Formalización del framework MAS** (2–3 semanas): extender `SAMComponent` a `CognitiveAgent` (creencias persistentes), sin romper `SANetwork`/`SATraining` existentes.
2. **Fase 2 — Agentes predictivos deterministas** (3–4 semanas): `StragglerPredictorAgent`, `AggregationSelectorAgent`, `ResourceBrokerAgent`.
3. **Fase 3 — Políticas aprendidas** (4 semanas): envolver neighbor-policies/training-policies con bandits contextuales, con las heurísticas actuales como fallback.
4. **Fase 4 — Integración con Groq** (2 semanas): `GroqReasoningAgent`, esquema de salida estructurada, pruebas de degradación segura ante fallos/latencia de la API.
5. **Fase 5 — Coordinación inter-nodo** (3–4 semanas): protocolo de negociación tipo contract-net sobre la capa de red existente.
6. **Fase 6 — Observabilidad y evaluación** (2 semanas): extender `nebula/frontend` para mostrar decisiones/explicaciones de agentes, correr escenarios comparativos.

---

## 10. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Latencia/indisponibilidad de la API de Groq afecta el ciclo de FL | Timeout agresivo + camino crítico nunca depende del LLM; degradación a agentes deterministas |
| Cambios en el catálogo/precios de modelos de Groq | Modelo configurable por JSON, nunca hardcodeado; revisión periódica de `console.groq.com/docs/models` |
| Fuga de información sensible en los prompts | Enviar solo metadatos agregados/anonimizados, nunca datos de entrenamiento ni pesos |
| Conflictos de reestructuración de topología entre nodos | Protocolo de negociación contract-net con timeout |
| Sobrecoste de invocar el LLM demasiado seguido | Invocación periódica (cada N rondas) + solo en empates de arbitraje, no en cada ronda |
| Complejidad añadida al codebase | Mantener el mismo contrato `SAMComponent`/`SACommand` para todo agente nuevo, sin introducir un framework paralelo |

---

*Documento preparado como ampliación técnica de la propuesta inicial de "Sistema Multi-Agente Adaptativo y Cognitivo para Aprendizaje Federado Dinámico", fundamentado en el código real del repositorio NEBULA (`nebula/core/situationalawareness/...`) y en la documentación pública de Groq vigente a septiembre de 2026.*
