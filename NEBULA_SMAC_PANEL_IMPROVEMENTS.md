# NEBULA · SMAC Panel: plan de mejora + agente que entiende las métricas

> Complementa `NEBULA_STYLE_GOVERNANCE.md` (v1.1). Usa los mismos tokens `--nb-*`.
> Alcance: (A) mejoras de UI/UX del SMAC Panel y (B) capa de datos y herramientas para que el agente lea, interprete y explique las métricas ya obtenidas de un escenario.

---

## 1. Diagnóstico de la pantalla actual

Estado observado: escenario `nebula_DFL_2026_09_28_00_53_33`, `completed`, `round 10`, `FedAvg`, 3 nodos (0, 1, 2), chat del agente abierto.

| # | Prioridad | Hallazgo | Consecuencia |
|---|---|---|---|
| D1 | Alta | El chat flotante tapa el panel **Control** (queda oculto un botón rojo) y el botón de envío de **Ask / command**. | Funciones inaccesibles mientras el chat está abierto. |
| D2 | Alta | El escenario está terminado pero **no hay ninguna métrica visible** (accuracy, loss, tiempos). Solo hay topología y controles, pensados para "en vivo". | No hay vista de resultados; el agente tampoco tiene nada que "mirar" en pantalla. |
| D3 | Alta | Hay **dos entradas al agente**: el chat y "Ask / command". Además **Cognitive reasoning** está vacío ("No agent decisions recorded yet"). | Duplicidad y confusión sobre cuál usar; el análisis posterior no existe. |
| D4 | Media | Escenario `completed` pero los 3 nodos aparecen grises como `offline/stopped` (contador `0 · 0 · 3`), y el nodo 0 no tiene aristas. | "Terminó bien" se confunde con "se cayó". Falta el estado `finished`. |
| D5 | Media | Los botones **Pause node** / **Resume node** están activos con "no node selected" y el escenario ya terminó. | Acciones sin sentido; el usuario no sabe por qué no pasa nada. |
| D6 | Media | Etiqueta `t+a` bajo cada nodo sin explicación (probablemente *trainer + aggregator*; confírmalo). | Información críptica. |
| D7 | Media | Idioma mixto: UI en inglés; agente y chips en español. | Experiencia inconsistente. |
| D8 | Media | Placeholder de Ask/command menciona "node 3" y "node 5", que **no existen** en este escenario (solo 0–2). | Ejemplos que inducen al error. |
| D9 | Media | Chips genéricos ("Nuevo escenario", "Ver estado") y saludo "¿Qué necesitas configurar?" en un escenario ya terminado. | El agente no parece entender el contexto. |
| D10 | Media | El **fondo decorativo de red** convive con el **grafo real de topología**: dos redes en pantalla, una sin significado. | Ruido y riesgo de leer datos donde no los hay. |
| D11 | Baja | Nombre del escenario truncado en el input; badges con estilos distintos; `round 10` sin total. | Legibilidad y consistencia. |
| D12 | Baja | Botones Pause/Resume en amarillo y verde saturados. | Pesan demasiado frente al principio minimalista. |

Lo que ya funciona y se conserva: leyenda de estados, botón de análisis, foco coral del input del chat, punto de estado del agente.

---

## 2. Mejoras de UI

### 2.1 Layout general

Pasar de "paneles sueltos + chat encima" a una rejilla con el chat como **tercera columna acoplable**.

```
┌ Header global ──────────────────────────────────────────────────────────┐
├ SMAC Panel · [Escenario ▾ …00_53_33]  ✔ completed · ronda 10/10 · FedAvg ┤
├──────────────────────────────┬──────────────────────────┬───────────────┤
│ Topología    [◀ ▶ ronda 7]   │ Métricas                 │ Agente        │
│  (replay con scrubber)       │ KPIs + curvas + tabla    │ (acoplado)    │
├──────────────────────────────┼──────────────────────────┤               │
│ Control (deshab. si terminó) │ Razonamiento cognitivo   │               │
└──────────────────────────────┴──────────────────────────┴───────────────┘
```

**Chat acoplable (resuelve D1)**

```css
.smac-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 24px;
}
.smac-layout[data-chat="docked"] {
  grid-template-columns: minmax(0, 1fr) 380px;
}
/* En modo acoplado el chat deja de ser fixed y pasa a ser una columna */
.smac-layout[data-chat="docked"] .nb-chat {
  position: sticky; top: 24px; right: auto; bottom: auto;
  height: calc(100vh - 120px); width: auto;
}
@media (max-width: 1279px) {
  /* Por debajo de 1280 px vuelve a ser overlay */
  .smac-layout[data-chat="docked"] { grid-template-columns: minmax(0, 1fr); }
  .smac-layout[data-chat="docked"] .nb-chat { position: fixed; inset: 0; height: auto; }
}
```

- Botón «Acoplar / Flotar» en la cabecera del chat (icono, `aria-label`, recuerda la preferencia en `localStorage`).
- Con el chat acoplado, el FAB se oculta.
- Con el chat flotante, se puede arrastrar y se recuerda su posición.

### 2.2 Cabecera del escenario (D11)

- Selector tipo *combobox* con escenarios recientes (más nuevo primero), búsqueda y botón «Copiar id». El nombre largo se abrevia por el centro (`nebula_DFL_2026…00_53_33`) con el completo en `title`.
- Badges unificados con los colores de estado del sistema (`--nb-ok`, `--nb-warn`, `--nb-error`) y neutro para el resto:
  `✔ completed` · `ronda 10/10` · `FedAvg` · `3 nodos`.
- «auto: on» pasa a interruptor con texto: «Actualización automática» (deshabilitado si el escenario terminó).

### 2.3 Topología (D4, D6, D10)

- **Nuevo estado `finished`**: nodo azul `--nb-blue-500` con check. Se distingue de `offline/stopped` (gris) y de `crashed` (rojo).

| Estado | Token | Marca |
|---|---|---|
| running | `--nb-ok` | punto relleno |
| paused | `--nb-warn` | icono pausa |
| finished | `--nb-blue-500` | check |
| offline / stopped | `--nb-text-muted` | anillo gris |
| malicious | `--nb-error` | icono de alerta |
| predicted straggler | `--nb-warn` | anillo discontinuo |

- Contador de estados con texto: `0 activos · 0 en pausa · 3 finalizados`.
- Sustituir `t+a` por icono de rol + tooltip («Trainer + Aggregator»).
- **Colorear por…**: selector `Estado | Accuracy | Loss | Tiempo de ronda`. Al elegir una métrica, el nodo usa una escala secuencial de azules.
- Grosor de arista proporcional al tráfico (bytes) cuando exista el dato.
- Nodo sin aristas: etiqueta «sin enlaces» y aviso en el panel de métricas (¿aislado desde qué ronda?).
- **Scrubber de ronda** debajo del grafo: mover la ronda actualiza topología, KPIs y marcador de las curvas (ver 3.4).
- **Quitar el fondo decorativo** en `/dashboard/*` (o dejarlo al ≤ 5 % de opacidad). Se mantiene solo en la landing.

### 2.4 Control (D5, D12)

- Si `status ≠ running`: todos los controles `disabled` + `title` con el motivo: «El escenario terminó; no hay nodos que controlar».
- Sin nodo seleccionado: acciones sobre nodo deshabilitadas con «Selecciona un nodo en la topología».
- Agrupar pares relacionados como un solo control: `[FedAvg ▾][Cambiar agregador]` y `[peer… ▾][Proponer desconexión]`.
- Botones **secundarios** (borde navy) con un punto de color en lugar de fondo saturado. Solo las acciones destructivas usan rojo y piden confirmación.
- Un solo botón primario por panel.

### 2.5 Razonamiento cognitivo y Ask / command (D3, D8)

**Unificar entradas.** El agente es uno solo: el chat. «Ask / command» pasa a ser una barra rápida que **envía al mismo chat** (misma `session_id`), lo abre si está cerrado y muestra ahí la respuesta.

```js
// Ask / command
askInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && askInput.value.trim()) {
    NebulaAgent.ask(askInput.value.trim(), { open: true });
    askInput.value = '';
  }
});
```

- Placeholder dinámico con nodos reales: `p. ej. «¿por qué el nodo 2 es lento?» o «pausa el nodo 1»`.
- **Cognitive reasoning** recibe las decisiones y los análisis del agente como entradas persistentes:

```
┌ 14:02 · ronda 7 · nodo 0 ───────────────── confianza: media ┐
│ Observación: accuracy del nodo 0 cae 9 pts respecto a la    │
│              mediana.                                        │
│ Hipótesis:   posible desbalance de datos local.              │
│ Evidencia:   [accuracy r5–r8] [evento: disconnect r7]        │
└──────────────────────────────────────────────────────────────┘
```

- Cada entrada muestra **observación** (dato), **hipótesis** (interpretación) y **evidencia** clicable que mueve el scrubber y resalta la curva.
- Estado vacío (escenario terminado): «Este escenario terminó sin intervenciones del agente.» + botón **Analizar resultados**.
- `Analyze state` cambia a `Analyze results` cuando el escenario ya terminó y ejecuta el análisis retrospectivo (sección 5.4).

### 2.6 Chat: saludo y sugerencias contextuales (D7, D9)

- **Idioma**: la UI usa i18n (`es` / `en`). El agente responde en el idioma en que escribe el usuario; chips y textos fijos siguen el idioma de la UI.
- Saludo según el estado del escenario:

| Estado | Saludo |
|---|---|
| `completed` | «Este escenario terminó en 10 rondas. ¿Quieres un resumen de resultados?» |
| `running` | «El escenario va por la ronda 4/10. Puedo vigilar nodos lentos o anomalías.» |
| sin escenario | «Elige un escenario para empezar.» |

- Chips contextuales (máx. 3):
  - `completed`: «Resumir resultados», «¿Qué nodo rindió peor?», «Comparar con otro escenario».
  - `running`: «¿Hay nodos lentos?», «Estado de la ronda», «Detectar anomalías».
  - Nodo seleccionado: «Explicar el nodo 2», «Ver su histórico».

### 2.7 Estados de carga, vacío y error

| Situación | Comportamiento |
|---|---|
| Cargando métricas | Esqueletos con la forma de cada gráfica; nunca spinner a pantalla completa. |
| Escenario sin métricas | «Este escenario no registró métricas. Comprueba que el logging esté activo.» |
| Fallo del proveedor | Mensaje con causa y acción: «No se pudo leer el escenario. Reintentar». |
| Métrica ausente para un nodo | Hueco en la curva (no interpolar) + nota «sin datos». |

---

## 3. Panel de métricas (nuevo)

### 3.1 Contenido

1. **Franja de KPIs** (5 máximo): accuracy final · loss final · mejor ronda · tiempo total · datos transmitidos. Cada uno con su variación respecto a la ronda 1.
2. **Curvas por ronda**: accuracy y loss. Línea gruesa navy = media global; líneas finas = nodos. El nodo seleccionado se resalta en coral.
3. **Tabla por nodo**: id · rol · accuracy final · loss final · tiempo medio de ronda · última ronda con actividad · estado final.
4. **Marcadores de eventos** (pausas, desconexiones, cambio de agregador, detección de nodo malicioso) como líneas verticales con tooltip.
5. Acciones por gráfica: **Explicar con el agente**, exportar CSV, exportar PNG.

### 3.2 Paleta de series

Series de nodos con tonos de la paleta existente; coral reservado para selección.

`#3E7CB1 · #2E9E6B · #D99A1B · #8E6BBF · #4FA3A5 · #7C8199`

Nunca depender solo del color: cada serie también lleva marcador distinto y etiqueta directa al final de la línea.

### 3.3 Reglas de gráficas

- Eje X = ronda (entero). Eje Y de accuracy en `0–1` o `0–100 %` (elegir uno y mantenerlo).
- Sin gradientes, sin sombras; rejilla en `--nb-line`.
- Máximo 6 nodos con línea individual; si hay más, mostrar banda min–max + media y resaltar solo el seleccionado.
- Animación solo al cambiar de métrica o de ronda (≤ 200 ms).

### 3.4 Scrubber de ronda

- Control único para todo el panel: `◀ ronda 7 / 10 ▶` con slider.
- Estado compartido con el agente (`ui_context.round_visible`), de modo que «¿qué pasó aquí?» se refiere a la ronda que el usuario está viendo.

---

## 4. Capa de datos: una única fuente de verdad

**Principio:** lo que el usuario ve y lo que el agente sabe salen del **mismo servicio**. Si un número aparece en pantalla, el agente puede citarlo, y viceversa.

```
Almacén de métricas (TensorBoard / CSV / DB)
        │
   MetricsProvider (adaptador)
        │
   MetricsService ──► API REST ──► UI (gráficas)
        │
   Digest (resumen precomputado)
        │
   Herramientas del agente ──► Agente
```

### 4.1 Localizar dónde están tus métricas

No pude verificar cómo almacena tu fork las métricas. Ejecuta esto en la carpeta de logs de un escenario terminado y adapta el proveedor al resultado:

```bash
find <ruta_logs>/nebula_DFL_2026_09_28_00_53_33 -maxdepth 3 -type f | head -50
# Busca: events.out.tfevents.* (TensorBoard) · *.csv · *.json · *.db / *.sqlite
```

| Si encuentras… | Proveedor |
|---|---|
| `events.out.tfevents.*` | `TensorBoardProvider` (`tensorboard.backend.event_processing.event_accumulator`) |
| `.csv` / `.json` por nodo | `FileProvider` |
| `.db` / `.sqlite` | `DbProvider` |

### 4.2 Esquema canónico

Formato *largo* (una fila por medición). Es simple de filtrar, de graficar y de exponer al agente.

```json
{
  "schema_version": "1.0",
  "scenario": {
    "id": "nebula_DFL_2026_09_28_00_53_33",
    "status": "completed",
    "started_at": "2026-09-28T00:53:33Z",
    "finished_at": "2026-09-28T01:20:10Z",
    "rounds_total": 10,
    "rounds_completed": 10,
    "aggregator": "FedAvg",
    "nodes": [
      { "id": 0, "role": "trainer+aggregator", "final_state": "stopped" },
      { "id": 1, "role": "trainer+aggregator", "final_state": "finished" },
      { "id": 2, "role": "trainer+aggregator", "final_state": "finished" }
    ]
  },
  "series": [
    { "metric": "accuracy", "scope": "node", "node_id": 1, "round": 3, "value": 0.81, "ts": "..." }
  ],
  "events": [
    { "ts": "...", "round": 4, "node_id": 0, "type": "disconnect", "source": "system", "detail": "..." }
  ]
}
```

> Los valores del ejemplo son ilustrativos; no son datos reales de tu escenario.

Reglas del esquema:
- `metric` en minúsculas y snake_case: `accuracy`, `loss`, `round_time_s`, `bytes_sent`, `bytes_received`.
- `scope`: `node` o `global`.
- `source` de los eventos: `user`, `agent` o `system`.
- Metadatos de cada métrica en un registro aparte: unidad y si *más alto es mejor*.

```json
{ "accuracy": { "unit": "ratio", "higher_is_better": true },
  "loss":     { "unit": "nats",  "higher_is_better": false },
  "round_time_s": { "unit": "s", "higher_is_better": false } }
```

### 4.3 Endpoints

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/scenarios` | Lista de escenarios recientes |
| GET | `/api/scenarios/{id}/summary` | Digest precomputado |
| GET | `/api/scenarios/{id}/metrics?metric=&node_ids=&round_from=&round_to=&max_points=` | Series |
| GET | `/api/scenarios/{id}/events?node_id=&types=` | Eventos |
| POST | `/api/scenarios/{id}/analysis` | Análisis retrospectivo del agente |
| POST | `/api/agent/chat` | Chat (ahora con `ui_context`) |

### 4.4 Digest: el resumen que el agente lee primero

El modelo **no debe recibir series crudas**. Se calcula un digest determinista cuando el escenario termina (y bajo demanda en vivo), y se cachea por `(scenario_id, última_modificación)`.

```python
# metrics/digest.py
import pandas as pd

METRIC_META = {
    "accuracy": {"higher_is_better": True},
    "loss": {"higher_is_better": False},
    "round_time_s": {"higher_is_better": False},
}

def _r(x, nd=4):
    return None if pd.isna(x) else round(float(x), nd)

def convergence_round(s: pd.Series, eps=0.005, k=3):
    """Primera ronda desde la que la variación entre rondas es < eps durante k rondas."""
    vals, rounds = s.to_list(), s.index.to_list()
    for i in range(1, len(vals) - k + 1):
        if all(abs(vals[j] - vals[j - 1]) < eps for j in range(i, i + k)):
            return int(rounds[i - 1])
    return None

def build_digest(series: pd.DataFrame, events: pd.DataFrame, meta: dict,
                 gap=0.05, straggler_ratio=1.5) -> dict:
    """series: [metric, scope, node_id, round, value, ts]
       events: [ts, round, node_id, type, source, detail]"""
    digest = {"schema_version": "1.0", "scenario": meta,
              "metrics": {}, "nodes": {}, "events": {}, "warnings": []}
    node_rows = series[series["scope"] == "node"]

    for metric, m in node_rows.groupby("metric"):
        hib = METRIC_META.get(metric, {}).get("higher_is_better", True)
        per_round = m.groupby("round")["value"].mean().sort_index()
        last = per_round.index.max()
        best = per_round.idxmax() if hib else per_round.idxmin()
        digest["metrics"][metric] = {
            "first": _r(per_round.iloc[0]),
            "final": _r(per_round.loc[last]),
            "delta": _r(per_round.loc[last] - per_round.iloc[0]),
            "best": {"round": int(best), "value": _r(per_round.loc[best])},
            "converged_at": convergence_round(per_round) if metric != "round_time_s" else None,
            "rounds_with_data": int(per_round.shape[0]),
        }
        # Ranking por nodo en la última ronda con datos
        final = m[m["round"] == last].set_index("node_id")["value"].sort_values(ascending=not hib)
        digest["metrics"][metric]["ranking_final"] = [
            {"node_id": int(n), "value": _r(v)} for n, v in final.items()]
        median = final.median()
        digest["metrics"][metric]["outliers"] = [
            {"node_id": int(n), "value": _r(v), "gap_vs_median": _r(v - median)}
            for n, v in final.items() if abs(v - median) > gap and metric != "round_time_s"]

    # Nodos lentos (tiempo medio de ronda vs mediana)
    if "round_time_s" in node_rows["metric"].values:
        t = node_rows[node_rows["metric"] == "round_time_s"].groupby("node_id")["value"].mean()
        digest["nodes"]["stragglers"] = [
            {"node_id": int(n), "mean_round_time_s": _r(v), "ratio_vs_median": _r(v / t.median(), 2)}
            for n, v in t.items() if v / t.median() > straggler_ratio]

    # Cobertura: nodos sin datos en la última ronda
    expected = {n["id"] for n in meta.get("nodes", [])}
    for metric, m in node_rows.groupby("metric"):
        missing = expected - set(m[m["round"] == m["round"].max()]["node_id"])
        if missing:
            digest["warnings"].append(
                f"{metric}: sin datos en la última ronda para los nodos {sorted(missing)}")

    if not events.empty:
        digest["events"] = {
            "count": int(len(events)),
            "by_type": events["type"].value_counts().to_dict(),
            "first_per_node": {int(n): {"round": int(g["round"].min()), "type": g.sort_values("ts").iloc[0]["type"]}
                               for n, g in events.dropna(subset=["node_id"]).groupby("node_id")},
        }
    return digest
```

Ajusta `gap` y `straggler_ratio` a tus escalas. Con solo 3 nodos, una razón contra la mediana es más estable que un z-score.

---

## 5. El agente y las métricas

### 5.1 Herramientas (function calling)

Todas son **de solo lectura** y llaman al mismo `MetricsService` que la UI. Formato compatible con la API de Anthropic (`input_schema`); adáptalo si usas otro proveedor.

```json
[
  {
    "name": "get_scenario_summary",
    "description": "Devuelve el resumen precomputado de un escenario: estado, rondas, métricas finales, mejor ronda, convergencia, ranking de nodos, valores atípicos, nodos lentos, eventos y avisos de datos. Llamar SIEMPRE primero para preguntas sobre resultados.",
    "input_schema": {
      "type": "object",
      "properties": { "scenario_id": { "type": "string" } },
      "required": ["scenario_id"]
    }
  },
  {
    "name": "get_metric_series",
    "description": "Serie de una métrica por ronda. Máx. 500 puntos; si hay más, se reduce. Usar solo cuando el resumen no basta.",
    "input_schema": {
      "type": "object",
      "properties": {
        "scenario_id": { "type": "string" },
        "metric": { "type": "string", "description": "accuracy, loss, round_time_s, bytes_sent…" },
        "node_ids": { "type": "array", "items": { "type": "integer" } },
        "round_from": { "type": "integer" },
        "round_to": { "type": "integer" }
      },
      "required": ["scenario_id", "metric"]
    }
  },
  {
    "name": "compare_rounds",
    "description": "Compara una métrica entre dos rondas, global y por nodo.",
    "input_schema": {
      "type": "object",
      "properties": {
        "scenario_id": { "type": "string" },
        "metric": { "type": "string" },
        "round_a": { "type": "integer" },
        "round_b": { "type": "integer" }
      },
      "required": ["scenario_id", "metric", "round_a", "round_b"]
    }
  },
  {
    "name": "get_events",
    "description": "Eventos del escenario (pausas, desconexiones, cambios de agregador, nodos maliciosos), filtrables por nodo, tipo y rango de rondas.",
    "input_schema": {
      "type": "object",
      "properties": {
        "scenario_id": { "type": "string" },
        "node_id": { "type": "integer" },
        "types": { "type": "array", "items": { "type": "string" } },
        "round_from": { "type": "integer" },
        "round_to": { "type": "integer" }
      },
      "required": ["scenario_id"]
    }
  },
  {
    "name": "compare_scenarios",
    "description": "Compara métricas finales de 2 a 4 escenarios (mismo dataset/modelo recomendado).",
    "input_schema": {
      "type": "object",
      "properties": {
        "scenario_ids": { "type": "array", "items": { "type": "string" }, "minItems": 2, "maxItems": 4 },
        "metric": { "type": "string" }
      },
      "required": ["scenario_ids", "metric"]
    }
  },
  {
    "name": "list_scenarios",
    "description": "Lista los escenarios recientes con estado y fecha.",
    "input_schema": { "type": "object", "properties": { "limit": { "type": "integer", "default": 10 } } }
  }
]
```

**Sobre de respuesta de las herramientas** (los errores deben ayudar al agente a corregirse solo):

```json
{ "ok": true,  "data": { }, "meta": { "source": "digest", "generated_at": "…", "truncated": false } }
{ "ok": false, "error": { "code": "NODE_NOT_FOUND", "message": "El nodo 7 no existe.", "valid_node_ids": [0, 1, 2] } }
{ "ok": false, "error": { "code": "METRIC_NOT_LOGGED", "message": "No hay 'f1'.", "available_metrics": ["accuracy", "loss"] } }
```

**Herramientas de control** (`pause_node`, `resume_node`, `switch_aggregator`, `propose_disconnect`): van en otro conjunto, requieren confirmación explícita del usuario en la UI y quedan **deshabilitadas si el escenario no está `running`**.

### 5.2 Contexto que viaja con cada mensaje

```json
POST /api/agent/chat
{
  "session_id": "uuid",
  "message": "¿Por qué el nodo 0 quedó sin enlaces?",
  "ui_context": {
    "page": "/dashboard/smac",
    "scenario_id": "nebula_DFL_2026_09_28_00_53_33",
    "scenario_status": "completed",
    "round_visible": 10,
    "selected_node": 0,
    "selected_metric": "accuracy",
    "locale": "es"
  }
}
```

El backend añade al *system prompt* una **ficha del escenario** (≈ 200 tokens: id, estado, rondas, agregador, nodos con estado final, métricas disponibles). Así el agente conoce lo básico sin gastar una llamada a herramienta y sabe qué nodos y métricas existen.

Puente de la UI con el agente:

```js
window.NebulaAgent = {
  ctx: {},
  setContext(patch) { this.ctx = { ...this.ctx, ...patch }; },
  ask(text, { open = false } = {}) { if (open) toggleChat(true); return send(text, this.ctx); }
};

// El panel actualiza el contexto al cambiar el estado visible
scrubber.addEventListener('input', e => NebulaAgent.setContext({ round_visible: +e.target.value }));
topology.on('nodeSelect', id => NebulaAgent.setContext({ selected_node: id }));
```

### 5.3 System prompt

```text
Eres el agente de NEBULA. Ayudas a interpretar y operar escenarios de aprendizaje federado descentralizado.

DATOS
- Los números que des DEBEN venir de resultados de herramientas o de la ficha del escenario. Nunca estimes, redondees de más ni completes de memoria.
- Empieza las preguntas sobre resultados con get_scenario_summary. Usa get_metric_series, compare_rounds o get_events solo si el resumen no basta.
- Si un nodo, ronda o métrica no existe, dilo y lista los valores válidos que devolvió la herramienta.
- Si faltan datos (huecos, nodos sin métricas), indícalo. No interpoles ni supongas.
- Distingue siempre entre lo que muestran los datos y tus hipótesis. Formula las hipótesis como posibles causas y di qué dato las confirmaría.

CONTEXTO
- Recibes ui_context con el escenario, la ronda visible y el nodo seleccionado. «Este nodo», «aquí» y «esta ronda» se refieren a eso.
- Si el escenario no está en ejecución (status ≠ running), no propongas pausar, reanudar ni desconectar. Puedes sugerir cambios para un nuevo escenario.

FORMATO
- Responde en el idioma del usuario.
- Primero la conclusión en una frase; después hasta 3 puntos de evidencia con ronda y nodo; al final, un siguiente paso opcional.
- Accuracy en porcentaje con 1–2 decimales; loss con 3–4 decimales; tiempos en segundos; siempre indica la ronda.
- Máximo una pregunta de aclaración, y solo si no puedes avanzar sin ella.
- Sin disculpas ni relleno.

ACCIONES
- Las acciones de control requieren confirmación del usuario. Describe qué harás y espera el «sí».
```

### 5.4 Análisis retrospectivo («Analizar resultados»)

1. La UI llama a `POST /api/scenarios/{id}/analysis`.
2. El backend obtiene el digest y lo pasa al agente con la instrucción de producir entradas para *Cognitive reasoning*.
3. El agente responde con JSON validado y el resultado se **persiste** con el hash del digest (si los datos no cambian, no se recalcula).

```json
{
  "entries": [
    {
      "round": 7, "node_id": 0,
      "observation": "…dato verificable…",
      "hypothesis": "…posible causa…",
      "confidence": "low|medium|high",
      "evidence": [
        { "type": "metric", "metric": "accuracy", "node_id": 0, "round_from": 5, "round_to": 8 },
        { "type": "event", "event_type": "disconnect", "round": 7 }
      ]
    }
  ],
  "summary": "…"
}
```

### 5.5 Respuestas enriquecidas en el chat

Se amplía el SSE del contrato original con referencias que la UI convierte en elementos interactivos.

```
event: token  data: {"text": "El nodo 0 perdió sus enlaces en la "}
event: ref    data: {"kind": "round", "value": 7}
event: ref    data: {"kind": "node", "value": 0}
event: card   data: {"type": "kpi", "items": [{"label": "Accuracy final", "value": "…"}]}
event: done   data: {}
```

- `ref round` → chip «ronda 7» que mueve el scrubber.
- `ref node` → chip «nodo 0» que selecciona el nodo en la topología.
- `card kpi | table` → tarjeta compacta dentro del mensaje (mismo estilo que la tarjeta de acción).
- Los botones **Explicar** de cada gráfica llaman a `NebulaAgent.ask("Explica esta curva…", { open: true })` con `selected_metric` y el rango visible.

### 5.6 Ejemplos de comportamiento esperado

Los datos son inventados, solo ilustran el patrón.

| Pregunta | Herramientas | Respuesta esperada |
|---|---|---|
| «¿Cómo terminó el escenario?» | `get_scenario_summary` | Conclusión con accuracy y loss finales, mejor ronda y ronda de convergencia. |
| «¿Qué nodo rindió peor?» | `get_scenario_summary` | Nodo con menor accuracy final, diferencia con la mediana, aviso si faltan datos. |
| «¿Por qué el nodo 0 no tiene enlaces?» | `get_events(node_id=0)` | Eventos de ese nodo con ronda y origen; si no hay evento, lo dice y no inventa causa. |
| «¿Cuál fue la accuracy del nodo 7?» | `get_metric_series(node_ids=[7])` → `NODE_NOT_FOUND` | «El nodo 7 no existe en este escenario; los nodos son 0, 1 y 2.» |
| «Dame el F1» | `get_scenario_summary` | «Este escenario solo registró accuracy y loss.» |
| «Pausa el nodo 1» (escenario terminado) | ninguna | «El escenario ya terminó; no hay nada que pausar.» Ofrece crear uno nuevo. |

### 5.7 Seguridad y límites

- Herramientas de lectura con verificación de propiedad del escenario en backend.
- Límite de 500 puntos por serie y de 20 llamadas de herramienta por turno.
- Nunca enviar logs crudos al modelo: solo digest y series acotadas.
- Timeout por herramienta y errores tipados (5.1).
- La clave del modelo vive solo en el servidor.
- Registrar cada llamada de herramienta (auditoría y depuración).

---

## 6. Evaluación del agente

Crear un **conjunto dorado** con un escenario de referencia y respuestas verificables por código.

| Categoría | Ejemplos | Criterio |
|---|---|---|
| Lectura de números | accuracy final, mejor ronda, loss inicial | Coincide exactamente con el digest |
| Comparación | nodo peor/mejor, ronda 3 vs 10 | Ranking y diferencias correctos |
| Causalidad prudente | «¿por qué cayó el nodo 0?» | Separa dato de hipótesis; no inventa |
| Entidades inexistentes | nodo 7, ronda 15, métrica F1 | Detecta y corrige con valores válidos |
| Contexto de UI | «¿y este nodo?» con `selected_node=2` | Usa el nodo correcto |
| Restricciones | pausar en escenario terminado | Se niega y explica |
| Idioma | pregunta en inglés | Responde en inglés |

**Meta:** ≥ 95 % de cifras correctas, 0 nodos o métricas inventados, 100 % de rechazos correctos en acciones no permitidas. Ejecutar en CI cada vez que cambie el *system prompt*, las herramientas o el modelo.

---

## 7. Hoja de ruta

| Fase | Contenido | Criterio de aceptación |
|---|---|---|
| **0 · Ajustes rápidos** (1–2 días) | Chat acoplable, controles deshabilitados en `completed`, estado `finished`, i18n, saludo y chips contextuales, placeholder dinámico, `ronda x/total`, quitar fondo de red en el panel. | Con el chat abierto no se tapa ningún control. Ningún texto mezcla idiomas. |
| **1 · Datos** | `MetricsProvider`, esquema canónico, endpoints, digest con caché. | `/summary` responde en < 500 ms con caché. Valores idénticos a los de las gráficas. |
| **2 · Agente** | Herramientas de lectura, ficha en el system prompt, `ui_context`, errores tipados. | Conjunto dorado ≥ 90 % (categorías de lectura y entidades inexistentes). |
| **3 · Panel de métricas** | KPIs, curvas, tabla, marcadores, scrubber, «Explicar». | Mover el scrubber actualiza topología, KPIs y contexto del agente. |
| **4 · Unificación** | Ask/command → chat; Cognitive reasoning con análisis persistente; referencias clicables. | No existe ninguna entrada al agente fuera del chat. «Analizar resultados» funciona en escenarios terminados. |
| **5 · Endurecimiento** | Evaluación en CI, auditoría de herramientas, límites, accesibilidad. | Conjunto dorado ≥ 95 %. Recorrido completo por teclado. |

---

## 8. Lista de verificación de esta pantalla

- [ ] Con el chat abierto (acoplado o flotante) todos los controles siguen accesibles.
- [ ] Un escenario `completed` muestra métricas y resultados; los controles de ejecución aparecen deshabilitados con motivo.
- [ ] `finished`, `offline/stopped` y `crashed` son visualmente distintos y están en la leyenda.
- [ ] Cada número del chat se puede localizar en pantalla (ronda y nodo clicables).
- [ ] Los ejemplos de placeholders y chips usan nodos que existen.
- [ ] Un solo idioma por sesión de UI; el agente sigue el idioma del usuario.
- [ ] Sin fondo decorativo de red sobre paneles de datos.
- [ ] Gráficas legibles sin depender del color (marcador + etiqueta directa).
- [ ] Contraste AA y foco visible en scrubber, chips, botones y tabla.
- [ ] Probado a 1280, 1440 y 1920 px, y en modo claro y oscuro del sistema.

---

## 9. Supuestos abiertos

1. **Dónde se guardan las métricas** en tu fork (sección 4.1). El proveedor se escribe según el resultado.
2. **Significado de `t+a`**: se asume *trainer + aggregator*.
3. **Métricas disponibles**: se asumen `accuracy` y `loss`; `round_time_s` y `bytes_*` mejoran el análisis de lentitud y tráfico, pero solo si ya se registran.
4. **Origen de las decisiones de Cognitive reasoning**: se asume que hoy no existe un registro persistente; si existe, se reutiliza en lugar de crear uno nuevo.

---

## 10. Control de cambios

| Versión | Fecha | Cambio |
|---|---|---|
| 1.0 | 29-09-2026 | Diagnóstico del SMAC Panel, plan de UI, capa de métricas y herramientas del agente |
