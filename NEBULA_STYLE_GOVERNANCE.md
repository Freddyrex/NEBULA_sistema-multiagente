# NEBULA · Gobernanza de estilo

> Guía de diseño para NEBULA (Platform for Decentralized Federated Learning).
> Objetivo: interfaz **minimalista, técnica y dinámica**, con un agente de IA accesible desde un chat flotante en cualquier pantalla.

---

## 1. Principios

1. **Claridad técnica.** NEBULA es una herramienta de investigación. Cada elemento comunica información, no decora.
2. **Una sola voz tipográfica.** Monoespaciada, en toda la interfaz. Es la firma visual actual y la mantenemos.
3. **Navy manda, coral señala.** El navy estructura; el coral se reserva para lo interactivo y lo importante.
4. **El movimiento responde a una acción.** Solo se anima lo que el usuario toca o lo que cambia de estado. El fondo de red es la única animación ambiental y es lenta.
5. **Un solo elemento memorable:** la red de nodos (fondo) y el chat del agente. Todo lo demás es silencioso.
6. **Accesible por defecto:** contraste AA, foco visible, `prefers-reduced-motion` respetado, funcional en móvil.

---

## 2. Color

Paleta derivada del logo y de la interfaz actual.

| Token | Hex | Uso |
|---|---|---|
| `--nb-navy-900` | `#1B1F3B` | Header, footer, botones primarios, texto de titulares |
| `--nb-navy-700` | `#1F3F6E` | Logo, enlaces secundarios, estados hover de navy |
| `--nb-blue-500` | `#3E7CB1` | Acentos de datos, gráficas, gradiente del logo |
| `--nb-blue-300` | `#4A90C2` | Extremo claro del gradiente, iconos activos |
| `--nb-coral-400` | `#FF8A65` | Enlaces, foco, elementos interactivos, indicador del agente |
| `--nb-surface-50` | `#F7F9FC` | Fondo de página |
| `--nb-surface-100` | `#EEF2F8` | Bandas de sección, paneles suaves |
| `--nb-ink-700` | `#3A3F55` | Texto de cuerpo |
| `--nb-ink-400` | `#7C8199` | Texto secundario, metadatos |
| `--nb-line` | `#D9DFEA` | Bordes y divisores |

**Estados**

| Token | Hex | Uso |
|---|---|---|
| `--nb-ok` | `#2E9E6B` | Nodo activo, entrenamiento completado |
| `--nb-warn` | `#D99A1B` | Nodo lento, advertencias |
| `--nb-error` | `#D64545` | Fallo de nodo, error de ronda |

**Reglas**
- El coral **no** se usa como fondo de bloques grandes. Solo en enlaces, foco, insignias y el punto de estado del agente.
- Texto coral sobre fondo claro no cumple AA para cuerpo pequeño: usa `--nb-coral-400` en fondos navy, y `#C4532F` para texto coral sobre fondos claros.
- Gradiente permitido (solo logo, barras de progreso y el botón del chat):
  `linear-gradient(135deg, #1F3F6E 0%, #4A90C2 100%)`

### Modo oscuro
Se invierten las superficies; el coral y el azul se mantienen.

> ⚠️ **Es opt-in.** Solo se activa con `data-theme="dark"` en `<html>`. **No** se activa por `prefers-color-scheme` hasta que todas las superficies del sitio (fondo del `body`, bandas, header, tarjetas, inputs) lean de los tokens `--nb-*`. Si solo cambian los colores de texto y el fondo sigue fijo en claro, queda texto claro sobre fondo claro (ilegible). Esto ocurrió en la v1.0.

| Token | Claro | Oscuro |
|---|---|---|
| `--nb-bg` | `#F7F9FC` | `#0F1226` |
| `--nb-surface` | `#EEF2F8` | `#171B36` |
| `--nb-text` | `#3A3F55` | `#DDE3F0` |
| `--nb-text-muted` | `#7C8199` | `#8E95B0` |
| `--nb-line` | `#D9DFEA` | `#2A2F52` |

---

## 3. Tipografía

**Familia única:** `JetBrains Mono` (fallback: `Fira Code`, `ui-monospace`, `Menlo`, `monospace`).

| Rol | Tamaño | Peso | Interlineado | Notas |
|---|---|---|---|---|
| Display (H1) | 40 px / 2.5rem | 500 | 1.15 | Un H1 por página |
| H2 | 28 px / 1.75rem | 500 | 1.25 | |
| H3 | 20 px / 1.25rem | 500 | 1.3 | |
| Cuerpo | 16 px / 1rem | 400 | 1.6 | Ancho máximo 72 caracteres |
| Pequeño / metadatos | 13 px / 0.8125rem | 400 | 1.5 | |
| Código inline | 14 px | 400 | 1.5 | Fondo `--nb-surface-100` |

**Reglas**
- Sentence case en títulos, botones y etiquetas. Evitar MAYÚSCULAS sostenidas (excepción: el logotipo).
- Sin subrayados decorativos; los enlaces se distinguen por color coral y subrayado al pasar el cursor.
- Cursiva solo para textos de apoyo (subtítulos bajo el H1).
- Escala de tamaños fluida en móvil: H1 baja a 28 px por debajo de 640 px.
- El ancho máximo de 72 caracteres se aplica a un contenedor de prosa (`.nb-prose`), **nunca** directamente a `p` ni a los bloques del hero. Todo bloque con `max-width` cuyo texto vaya centrado debe llevar `margin-inline: auto`; si no, el bloque se pega a la izquierda y el texto queda descentrado.
- H1 fluido para evitar saltos de línea forzados por el contenedor de 1200 px: `font-size: clamp(1.75rem, 3vw, 2.5rem)`.

---

## 4. Espaciado, forma y elevación

**Escala de espaciado (base 4 px):** `4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96`

**Contenedor:** ancho máximo `1200 px`, centrado, padding lateral `24 px` (16 px en móvil).

**Radios**
| Elemento | Radio |
|---|---|
| Botones, inputs | `6 px` |
| Paneles y tarjetas | `12 px` |
| Panel del chat | `16 px` |
| Botón flotante, avatares, insignias | `999 px` |

**Bordes antes que sombras.** La jerarquía se marca con borde `1 px solid var(--nb-line)`. Una única sombra permitida, para elementos flotantes (chat, menús, modales):
`0 8px 24px rgba(27, 31, 59, 0.14)`

---

## 5. Componentes base

### Botón primario
- Fondo `--nb-navy-900`, texto blanco, radio 6 px, padding `10px 20px`.
- Hover: fondo `--nb-navy-700`. Activo: `translateY(1px)`. Foco: anillo coral 2 px con offset 2 px.
- El texto del botón nombra la acción: «Enviar comentarios», «Iniciar entrenamiento», no «Enviar».

### Botón secundario
- Borde `1.5 px` navy, fondo transparente, texto navy. Hover: fondo `--nb-surface-100`.

### Enlace
- Coral (`#C4532F` en claro, `--nb-coral-400` en oscuro). Subrayado al hover.

### Input / textarea
- Borde `--nb-line`, radio 6 px. Foco: borde coral + anillo `0 0 0 3px rgba(255,138,101,.25)`.
- Placeholder en `--nb-ink-400`.

### Header
- Altura 80 px, fondo `--nb-navy-900`, logo a la izquierda, navegación a la derecha.
- Enlace activo con línea inferior coral de 2 px.

### Insignias de estado (nodos, rondas)
- Punto de 8 px + texto. Colores `--nb-ok`, `--nb-warn`, `--nb-error`. Nunca solo color: siempre acompañado de texto.

---

## 6. Fondo de red (elemento firma)

- Canvas o SVG de nodos y aristas, color `--nb-blue-500` al **12–18 % de opacidad**.
- Movimiento muy lento (deriva de 0.1–0.3 px/frame), máximo 60 nodos en escritorio y 25 en móvil.
- Nunca detrás de texto largo o formularios densos: en esas zonas, capa `--nb-bg` al 92 %.
- Con `prefers-reduced-motion: reduce`, el fondo queda estático.
- Interacción opcional: los nodos cercanos al cursor se conectan a él (radio 120 px).

---

## 7. Movimiento

| Tipo | Duración | Curva |
|---|---|---|
| Micro (hover, foco, toggle) | 120–160 ms | `ease-out` |
| Apertura de paneles | 220–280 ms | `cubic-bezier(.2, .8, .2, 1)` |
| Entrada de mensajes del chat | 180 ms | `ease-out` |
| Fondo ambiental | continuo, lento | lineal |

Reglas:
- Solo animar `transform` y `opacity`.
- Sin animaciones de entrada en cada sección al hacer scroll.
- `@media (prefers-reduced-motion: reduce)` desactiva todo salvo cambios de estado instantáneos.

---

## 8. Chat flotante del agente

El agente vive en un **globo flotante** presente en toda la plataforma. Debe sentirse parte de NEBULA, no un widget de terceros.

### 8.1 Botón flotante (FAB)

| Propiedad | Valor |
|---|---|
| Posición | `fixed`, `bottom: 24px; right: 24px` (16 px en móvil) |
| Tamaño | 56 × 56 px, circular |
| Fondo | gradiente del logo (`#1F3F6E → #4A90C2`) |
| Icono | Burbuja de chat con un nodo (tres puntos conectados), blanco |
| Punto de estado | 10 px, coral cuando el agente tiene algo que decir; verde cuando está listo |
| Sombra | la única sombra flotante (ver §4) |
| Hover | `scale(1.06)` |
| z-index | `1000` |

Estados: **cerrado**, **abierto** (el icono cambia a «×» con rotación de 90°), **con notificación** (punto coral pulsando una sola vez, no en bucle).

### 8.2 Panel de conversación

| Propiedad | Valor |
|---|---|
| Tamaño escritorio | 380 × 560 px (máx. `calc(100vh - 120px)`) |
| Tamaño móvil | Pantalla completa, con barra superior propia |
| Posición | Anclado sobre el FAB, `bottom: 96px; right: 24px` |
| Fondo | `--nb-bg`, borde `--nb-line`, radio 16 px |
| Apertura | `opacity 0→1` + `translateY(12px→0)` + `scale(.98→1)`, 240 ms, origen abajo a la derecha |

**Estructura**
```
┌──────────────────────────────┐
│ ● Agente NEBULA        —  ×  │  ← Cabecera navy, punto de estado
├──────────────────────────────┤
│                              │
│  [agente] Hola. ¿Qué         │
│  necesitas configurar?       │
│                              │
│           [tú] Crear un      │
│           escenario con 5    │
│           nodos              │
│                              │
│  [agente] ▍ escribiendo…     │
│                              │
├──────────────────────────────┤
│ Sugerencias: [Nuevo escenario] [Ver estado] │
├──────────────────────────────┤
│ [ Escribe un mensaje…    ] ➤ │
└──────────────────────────────┘
```

**Mensajes**
- Agente: fondo `--nb-surface-100`, texto `--nb-ink-700`, alineado a la izquierda, radio `12px 12px 12px 4px`.
- Usuario: fondo `--nb-navy-900`, texto blanco, alineado a la derecha, radio `12px 12px 4px 12px`.
- Ancho máximo del mensaje: 85 %. Fuente 14 px.
- Bloques de código: fondo `#0F1226`, texto `#DDE3F0`, botón «Copiar» al hover.
- Respuestas en streaming: el texto aparece de forma incremental; indicador de «escribiendo» con tres puntos (opacidad alterna, 1.2 s).
- Acciones del agente (por ejemplo «lanzó un entrenamiento») se muestran como **tarjeta de acción** con borde izquierdo coral de 3 px, nombre de la acción y resultado.

**Cabecera**
- Fondo `--nb-navy-900`, título «Agente NEBULA» en blanco, punto de estado (verde: listo, coral: procesando, rojo: sin conexión).
- Botones: minimizar y cerrar, con `aria-label`.

**Chips de sugerencias**
- Borde 1 px `--nb-line`, radio 999 px, 13 px. Hover: borde coral. Máximo 3 visibles; sirven de guía inicial y desaparecen tras el primer mensaje.

### 8.3 Textos del agente (voz)

- Voz directa, en segunda persona, sin relleno ni disculpas exageradas.
- Los errores explican qué pasó y qué hacer: «No se pudo conectar con el nodo 3. Comprueba que esté en línea y vuelve a intentarlo».
- Estado vacío: «Pregúntame por tus escenarios, nodos o resultados».
- Placeholder del input: «Escribe un mensaje…».
- Una acción mantiene el mismo nombre en toda la interfaz: si el botón dice «Iniciar entrenamiento», el agente dice «Entrenamiento iniciado».

### 8.4 Accesibilidad del chat

- FAB: `<button aria-label="Abrir chat con el agente" aria-expanded="false" aria-controls="nb-chat">`.
- Panel: `role="dialog" aria-label="Chat con el agente NEBULA"`; foco pasa al input al abrir y vuelve al FAB al cerrar.
- Lista de mensajes: `role="log" aria-live="polite"`.
- Atajos: `Esc` cierra el panel; `Enter` envía; `Shift+Enter` salto de línea.
- Contraste mínimo AA en todos los estados.

### 8.5 Contrato de integración con el agente

Endpoint sugerido (ajústalo a tu backend):

```http
POST /api/agent/chat
Content-Type: application/json

{
  "session_id": "uuid",
  "message": "Crea un escenario con 5 nodos",
  "context": { "page": "/dashboard", "scenario_id": null }
}
```

Respuesta por **streaming (SSE)** con eventos tipados:

```
event: token      data: {"text": "Claro, "}
event: action     data: {"name": "create_scenario", "status": "done", "detail": "5 nodos"}
event: done       data: {}
event: error      data: {"message": "…"}
```

Recomendaciones:
- Enviar `context.page` para que el agente sepa dónde está el usuario.
- Guardar el historial por `session_id` (localStorage solo para el id, el historial en servidor).
- Limitar tasa de peticiones y validar sesión en backend; **nunca** exponer claves de API en el frontend.
- Confirmar con el usuario las acciones destructivas o costosas (borrar escenario, lanzar entrenamiento) antes de ejecutarlas.

---

## 9. Implementación de referencia

### 9.1 Tokens CSS

```css
:root {
  --nb-navy-900: #1B1F3B;
  --nb-navy-700: #1F3F6E;
  --nb-blue-500: #3E7CB1;
  --nb-blue-300: #4A90C2;
  --nb-coral-400: #FF8A65;
  --nb-coral-600: #C4532F;

  --nb-bg: #F7F9FC;
  --nb-surface: #EEF2F8;
  --nb-text: #3A3F55;
  --nb-text-muted: #7C8199;
  --nb-line: #D9DFEA;

  --nb-ok: #2E9E6B;
  --nb-warn: #D99A1B;
  --nb-error: #D64545;

  --nb-font: "JetBrains Mono", "Fira Code", ui-monospace, Menlo, monospace;
  --nb-shadow-float: 0 8px 24px rgba(27, 31, 59, .14);
  --nb-ease: cubic-bezier(.2, .8, .2, 1);
}

/* Modo oscuro: SOLO opt-in con data-theme="dark".
   No usar prefers-color-scheme hasta migrar todas las superficies a los tokens. */
:root[data-theme="dark"] {
  --nb-bg: #0F1226;
  --nb-surface: #171B36;
  --nb-text: #DDE3F0;
  --nb-text-muted: #8E95B0;
  --nb-line: #2A2F52;
}

body {
  font-family: var(--nb-font);
  background: var(--nb-bg);
  color: var(--nb-text);
  line-height: 1.6;
}

:focus-visible {
  outline: 2px solid var(--nb-coral-400);
  outline-offset: 2px;
}
```

### 9.2 Chat flotante (HTML)

```html
<button id="nb-chat-fab" class="nb-fab" aria-label="Abrir chat con el agente"
        aria-expanded="false" aria-controls="nb-chat">
  <svg class="nb-fab__icon" viewBox="0 0 24 24" width="26" height="26" fill="none"
       stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
       aria-hidden="true">
    <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z"/>
    <circle cx="9" cy="12" r="1" fill="currentColor"/>
    <circle cx="15" cy="9.5" r="1" fill="currentColor"/>
    <circle cx="15" cy="14.5" r="1" fill="currentColor"/>
    <path d="M9 12l6-2.5M9 12l6 2.5"/>
  </svg>
  <span class="nb-fab__dot" aria-hidden="true"></span>
</button>

<section id="nb-chat" class="nb-chat" role="dialog" aria-label="Chat con el agente NEBULA" hidden>
  <header class="nb-chat__head">
    <span class="nb-chat__status" aria-hidden="true"></span>
    <strong>Agente NEBULA</strong>
    <button class="nb-chat__close" aria-label="Cerrar chat">×</button>
  </header>

  <div class="nb-chat__log" role="log" aria-live="polite"></div>

  <div class="nb-chat__chips">
    <button class="nb-chip">Nuevo escenario</button>
    <button class="nb-chip">Ver estado</button>
  </div>

  <div class="nb-chat__input">
    <textarea rows="1" placeholder="Escribe un mensaje…" aria-label="Mensaje"></textarea>
    <button class="nb-chat__send" aria-label="Enviar mensaje">➤</button>
  </div>
</section>
```

### 9.3 Chat flotante (CSS)

```css
.nb-fab {
  position: fixed; right: 24px; bottom: 24px; z-index: 1000;
  width: 56px; height: 56px; border: 0; border-radius: 999px; cursor: pointer;
  color: #fff;
  background: linear-gradient(135deg, var(--nb-navy-700), var(--nb-blue-300));
  box-shadow: var(--nb-shadow-float);
  display: grid; place-items: center;
  transition: transform 140ms ease-out;
}
.nb-fab:hover { transform: scale(1.06); }
.nb-fab__dot {
  position: absolute; top: 6px; right: 6px; width: 10px; height: 10px;
  border-radius: 999px; background: var(--nb-ok); border: 2px solid var(--nb-bg);
}
.nb-fab[data-notify="true"] .nb-fab__dot { background: var(--nb-coral-400); }

.nb-chat {
  position: fixed; right: 24px; bottom: 96px; z-index: 1000;
  width: 380px; height: min(560px, calc(100vh - 120px));
  display: flex; flex-direction: column;
  background: var(--nb-bg); color: var(--nb-text);
  border: 1px solid var(--nb-line); border-radius: 16px;
  box-shadow: var(--nb-shadow-float);
  transform-origin: bottom right;
  opacity: 0; transform: translateY(12px) scale(.98);
  transition: opacity 240ms var(--nb-ease), transform 240ms var(--nb-ease);
}
.nb-chat[data-open="true"] { opacity: 1; transform: none; }
.nb-chat[hidden] { display: none; }

.nb-chat__head {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 16px; background: var(--nb-navy-900); color: #fff;
  border-radius: 16px 16px 0 0;
}
.nb-chat__status { width: 8px; height: 8px; border-radius: 999px; background: var(--nb-ok); }
.nb-chat__head strong { flex: 1; font-weight: 500; font-size: 14px; }
.nb-chat__close { background: none; border: 0; color: #fff; font-size: 20px; cursor: pointer; }

.nb-chat__log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.nb-msg { max-width: 85%; padding: 10px 14px; font-size: 14px; animation: nb-in 180ms ease-out; }
.nb-msg--agent { align-self: flex-start; background: var(--nb-surface); border-radius: 12px 12px 12px 4px; }
.nb-msg--user  { align-self: flex-end; background: var(--nb-navy-900); color: #fff; border-radius: 12px 12px 4px 12px; }
.nb-msg--action { border-left: 3px solid var(--nb-coral-400); border-radius: 6px; background: var(--nb-surface); }
@keyframes nb-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

.nb-chat__chips { display: flex; gap: 8px; flex-wrap: wrap; padding: 0 16px 12px; }
.nb-chip {
  font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 999px;
  background: transparent; color: var(--nb-text); border: 1px solid var(--nb-line); cursor: pointer;
  transition: border-color 140ms ease-out;
}
.nb-chip:hover { border-color: var(--nb-coral-400); }

.nb-chat__input { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--nb-line); }
.nb-chat__input textarea {
  flex: 1; resize: none; font: inherit; font-size: 14px; padding: 10px 12px;
  color: var(--nb-text); background: var(--nb-bg);
  border: 1px solid var(--nb-line); border-radius: 6px;
}
.nb-chat__input textarea:focus { outline: none; border-color: var(--nb-coral-400); box-shadow: 0 0 0 3px rgba(255,138,101,.25); }
.nb-chat__send { border: 0; border-radius: 6px; padding: 0 14px; color: #fff; background: var(--nb-navy-900); cursor: pointer; }
.nb-chat__send:hover { background: var(--nb-navy-700); }

@media (max-width: 640px) {
  .nb-fab { right: 16px; bottom: 16px; }
  .nb-chat { inset: 0; width: auto; height: auto; border-radius: 0; border: 0; }
  .nb-chat__head { border-radius: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .nb-fab, .nb-chat, .nb-msg, .nb-chip { transition: none; animation: none; }
}
```

### 9.4 Chat flotante (JS, con streaming SSE)

```js
const fab = document.getElementById('nb-chat-fab');
const chat = document.getElementById('nb-chat');
const log = chat.querySelector('.nb-chat__log');
const input = chat.querySelector('textarea');
const sessionId = localStorage.getItem('nb_session') ?? crypto.randomUUID();
localStorage.setItem('nb_session', sessionId);

function toggleChat(open) {
  chat.hidden = false;
  requestAnimationFrame(() => chat.dataset.open = String(open));
  fab.setAttribute('aria-expanded', String(open));
  if (open) input.focus(); else { fab.focus(); setTimeout(() => chat.hidden = true, 240); }
}
fab.addEventListener('click', () => toggleChat(chat.dataset.open !== 'true'));
chat.querySelector('.nb-chat__close').addEventListener('click', () => toggleChat(false));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && chat.dataset.open === 'true') toggleChat(false); });

function addMsg(role, text = '') {
  const el = document.createElement('div');
  el.className = `nb-msg nb-msg--${role}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function send(message) {
  addMsg('user', message);
  const reply = addMsg('agent');
  const res = await fetch('/api/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, context: { page: location.pathname } })
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop();
    for (const raw of events) {
      const type = /event: (\w+)/.exec(raw)?.[1];
      const data = JSON.parse(/data: (.*)/.exec(raw)?.[1] ?? '{}');
      if (type === 'token') reply.textContent += data.text;
      if (type === 'action') addMsg('agent', `${data.name}: ${data.detail}`).classList.add('nb-msg--action');
      if (type === 'error') reply.textContent = data.message;
      log.scrollTop = log.scrollHeight;
    }
  }
}

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && input.value.trim()) {
    e.preventDefault();
    send(input.value.trim());
    input.value = '';
  }
});
```

---

## 10. Lista de verificación antes de publicar una pantalla

- [ ] Solo tipografía monoespaciada, con la escala de la §3.
- [ ] Coral usado únicamente en interacción, foco o estado del agente.
- [ ] Contraste AA en texto y controles (claro y oscuro).
- [ ] Foco visible en todos los elementos interactivos.
- [ ] Sin sombras salvo en elementos flotantes.
- [ ] Movimiento limitado a `transform` y `opacity`, con `prefers-reduced-motion` respetado.
- [ ] Chat flotante presente, sin tapar acciones principales en móvil.
- [ ] Textos en sentence case, en voz activa y con la misma denominación para cada acción en toda la interfaz.
- [ ] Probado a 360 px, 768 px y 1440 px.
- [ ] Probado con el sistema operativo en modo claro **y** en modo oscuro.
- [ ] Todo `max-width` en bloques con texto centrado lleva `margin-inline: auto`.
- [ ] Ningún color de texto o fondo está fijado a mano fuera de los tokens `--nb-*`.

---

## 11. Control de cambios

| Versión | Fecha | Cambio |
|---|---|---|
| 1.0 | 29-09-2026 | Primera versión: tokens, componentes, fondo de red y chat del agente |
| 1.1 | 29-09-2026 | Corrección: modo oscuro pasa a opt-in (texto claro sobre fondo claro); regla de centrado con `max-width`; H1 fluido |

Cualquier cambio en tokens o componentes se propone por PR con captura antes/después y se anota en esta tabla.
