# SDD — EPIC-001: Signal Quality & Edge Validation

**Fecha:** 2026-06-04
**Estado:** En progreso
**Autor:** outis10

---

## 1. Objetivo

Elevar la calidad de señales y añadir los mecanismos de validación de edge necesarios
para que el sistema pueda operar con confianza en producción.

Este EPIC surge del review arquitectónico del sistema (ver sección 4) e implementa
las mejoras de mayor impacto antes del go-live.

---

## 2. Alcance

### Incluido

| # | Issue | Descripción |
|---|-------|-------------|
| 1 | [#1] | EV threshold diferenciado paper/live (ADR-006) |
| 2 | [#2] | Persistir bid/ask/fill estimado en SignalORM y BetORM |
| 3 | [#3] | Job de closing price para calcular CLV (ADR-005) |
| 4 | [#4] | Pre-kickoff lineup check + recálculo de modelo |
| 5 | [#5] | Settlement multi-fuente (API-Football + Polymarket market status) |

### Fuera de alcance (EPIC futuro)

- Reemplazar Dixon-Coles por modelo basado en xG como input principal
- EV-based stop-loss (requiere pasar ModelPrediction al PositionManager — alta complejidad)
- Priors de liga por separado (requiere data propia acumulada)
- Dashboard de métricas (Grafana/Metabase)

---

## 3. Decisiones arquitectónicas relacionadas

| ADR | Título | Estado |
|-----|--------|--------|
| [ADR-005](../adr/ADR-005-clv-metrica-primaria.md) | CLV como métrica primaria | Aceptado |
| [ADR-006](../adr/ADR-006-ev-threshold-diferenciado.md) | EV threshold diferenciado | Propuesto |

---

## 4. Contexto — Review arquitectónico

El review identificó los siguientes gaps (en orden de prioridad):

### Gap 1: EV threshold sin diferenciación paper/live
**Archivo:** `src/sporting_edge/agents/odds_analyzer.py:150`
**Problema:** `MIN_EV_THRESHOLD=5%` aplica igual en paper y producción.
Con fricciones reales (spread 2-4%, slippage 0.5-1.5%, error de modelo 3-5%),
operar con solo 5% de EV implica EV neto negativo.
**Solución:** `min_ev_threshold_live=8%` en producción (ver ADR-006).

### Gap 2: Métricas de señal incompletas
**Archivo:** `src/sporting_edge/agents/odds_analyzer.py:241` (`_persist_signal`)
**Problema:** `SignalORM` no guarda `yes_bid`, `yes_ask`, `estimated_fill_price`,
ni `actual_fill_price`. Sin estos datos no se puede calcular CLV.
**Solución:** Extender `SignalORM` y `BetORM` + nueva migración SQL.

### Gap 3: CLV no implementado
**Problema:** No existe job que capture el precio de cierre antes del kickoff.
CLV es la métrica más valiosa para detectar edge real con muestras pequeñas.
**Solución:** Job APScheduler que corre ~10 min antes del kickoff de cada partido
con bets abiertos, registra el `closing_price` del CLOB, calcula CLV.

### Gap 4: Cierre pre-kickoff sin alineaciones
**Archivo:** `src/sporting_edge/agents/position_manager.py:99`
**Problema:** Force-close a 60 min sin validar si el edge sigue presente.
Las alineaciones se publican ~60 min antes y son el evento informativo más importante.
**Solución:** A 65 min antes, obtener lineups de API-Football, recalcular modelo,
y solo cerrar si EV cayó < threshold. Si sigue con edge, mantener hasta 30 min.

### Gap 5: Settlement fuente única
**Archivo:** `src/sporting_edge/agents/bet_settler.py:134`
**Problema:** Solo consulta API-Football. Casos edge: partidos suspendidos,
AWD (award), walkover pueden resolverse diferente en Polymarket.
**Solución:** Cruzar con `/markets/{condition_id}` de Polymarket para confirmar
el estado de resolución del mercado.

---

## 5. Modelo de datos — Cambios

### `SignalORM` (tabla `signals`)
```sql
-- Nuevas columnas
yes_bid         NUMERIC(8,4)   -- best bid al momento de la señal
yes_ask         NUMERIC(8,4)   -- best ask al momento de la señal
estimated_fill  NUMERIC(8,4)   -- avg fill estimado por estimate_fill()
book_liquidity  NUMERIC(12,2)  -- liquidez total del ask side al momento
```

### `BetORM` (tabla `bets`)
```sql
-- Nuevas columnas
actual_fill_price  NUMERIC(8,4)   -- precio real de fill del CLOB
closing_price      NUMERIC(8,4)   -- precio del CLOB ~10 min antes del kickoff
clv                NUMERIC(8,4)   -- closing_price - entry_price (positivo = bueno)
```

---

## 6. Definición de Done (DoD)

- [ ] Todos los issues del alcance cerrados
- [ ] Tests unitarios para lógica nueva (EV threshold, CLV cálculo, lineup parsing)
- [ ] Nueva migración SQL aplicada
- [ ] ADR-006 actualizado a "Aceptado"
- [ ] README actualizado con nuevas variables de configuración
- [ ] Al menos 1 ciclo de paper trading completo con las nuevas métricas registradas

---

## 7. Historial de cambios

| Fecha | Cambio |
|-------|--------|
| 2026-06-04 | Documento inicial creado tras review arquitectónico |
