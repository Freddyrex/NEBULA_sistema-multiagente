# ADR 0001 — Capa multiagente opcional para SMAC

- **Estado:** aceptado
- **Fecha:** 2026-10-27
- **Ámbito:** controller/copiloto y nodo agregador

## Contexto

NEBULA ya dispone de agentes deterministas y de un asesor cognitivo Gemini
(`GeminiReasoning`) que compite en la arbitración del nodo. El plan
`PLAN_AGENTES_CREWAI_NEBULA.md` evaluó una capa CrewAI con especialistas
(arquitectura, rendimiento, seguridad, redes y coste) y varias estrategias de
decisión. El paper de referencia muestra que `role-based` sobreactiva patrones,
mientras que `Voting`/`Debate` logran mejor equilibrio; una ronda multiagente
completa puede costar 44.6–53.6 s, incompatible con el operador interactivo.

## Decisión

1. La capa vive en `nebula/agentic/` y es **opcional**. El flag
   `adaptive_agent.multi_agent` (o `SMAC_MULTI_AGENT`) está apagado por defecto.
2. Estrategia por defecto `auto`: fast-path determinista → Voting; `role` con
   hallazgos críticos o más de dos dominios; `debate` solo opt-in.
3. Presupuesto duro de **8 s p95** (`SMAC_CREW_TOTAL_TIMEOUT_S`), con caché por
   `state_hash`, fallback a `carry_over` y degradación al flujo single-agent si
   el LLM no está disponible.
4. El crew **solo propone**. Toda acción pasa por guardrails deterministas y
   queda pendiente de confirmación humana/RBAC; nunca ejecuta directamente.
5. Solo se permiten palancas reales de NEBULA: agregador, pausa/reanudación,
   rondas/epochs, modelo, desconexión y nueva simulación con coste. Patrones no
   implementados (HDH/GAN/JSD, compresión) quedan `available=False`.
6. CrewAI se instala como grupo opcional `crewai`; si no está presente se usa
   un backend Gemini REST de una sola llamada con el mismo contrato.
7. El nodo agregador reutiliza el contrato y mapea la propuesta a `SACommand`;
   la arbitración existente y el veto de seguridad se mantienen intactos.

## Consecuencias

- El flujo actual no cambia con el flag apagado.
- La latencia queda acotada; se sacrifica profundidad de debate por defecto.
- Las dependencias de CrewAI no entran en core/edge.
- Toda decisión queda auditable en `crew_trace.jsonl` y en el endpoint
  `GET /platform/api/smac/crew/trace`.
- La calidad depende de la evidencia del snapshot; sin evidencia los guardrails
  vetan la propuesta.
