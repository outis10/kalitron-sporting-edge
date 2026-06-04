# SDD — EPIC-001: Signal Quality & Edge Validation

**Fecha:** 2026-06-04
**Completado:** 2026-06-04
**Estado:** Completado
**Autor:** outis10

---

## 1. Objetivo

Elevar la calidad de señales y añadir los mecanismos de validación de edge necesarios
para que el sistema pueda operar con confianza en producción.

Este EPIC surge del review arquitectónico del sistema (ver sección 4) e implementa
las mejoras de mayor impacto antes del go-live.

---

## 2. Alcance

### Entregado

| # | Issue | PR | Descripción | Estado |
|---|-------|----|-------------|--------|
| 1 | [#2](https://github.com/outis10/kalitron-sporting-edge/issues/2) | [#7](https://github.com/outis10/kalitron-sporting-edge/pull/7) | EV threshold diferenciado paper/live | ✅ Merged |
| 2 | [#3](https://github.com/outis10/kalitron-sporting-edge/issues/3) | [#9](https://github.com/outis10/kalitron-sporting-edge/pull/9) | Persistir bid/ask/fill en SignalORM y BetORM | ✅ Merged |
| 3 | [#4](https://github.com/outis10/kalitron-sporting-edge/issues/4) | [#10](https://github.com/outis10/kalitron-sporting-edge/pull/10) | Job CLV tracker — closing price antes del kickoff | ✅ Merged |
| 4 | [#5](https://github.com/outis10/kalitron-sporting-edge/issues/5) | [#11](https://github.com/outis10/kalitron-sporting-edge/pull/11) | Settlement multi-fuente (API-Football + Polymarket) | ✅ Merged |
| 5 | [#6](https://github.com/outis10/kalitron-sporting-edge/issues/6) | [#12](https://github.com/outis10/kalitron-sporting-edge/pull/12) | Pre-kickoff lineup check + two-stage close | ✅ Merged |

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
| [ADR-006](../adr/ADR-006-ev-threshold-diferenciado.md) | EV threshold diferenciado | Aceptado |

---

## 4. Contexto — Review arquitectónico

El review identificó los siguientes gaps (en orden de prioridad):

### Gap 1: EV threshold sin diferenciación paper/live
**Archivo:** `src/sporting_edge/agents/odds_analyzer.py`
**Problema:** `MIN_EV_THRESHOLD=5%` aplica igual en paper y producción.
Con fricciones reales (spread 2-4%, slippage 0.5-1.5%, error de modelo 3-5%),
operar con solo 5% de EV implica EV neto negativo.
**Solución:** `_ev_threshold(liquidity)` selecciona 5% (paper), 8% (live, liquidez normal),
o 12% (live, liquidez < $10k). Ver ADR-006.

### Gap 2: Métricas de señal incompletas
**Archivo:** `src/sporting_edge/agents/odds_analyzer.py` (`_persist_signal`)
**Problema:** `SignalORM` no guardaba `clob_bid`, `clob_ask`, `estimated_fill_price`,
ni `book_liquidity_usd`. Sin estos datos no se puede calcular CLV.
**Solución:** `_enrich_with_clob_prices()` post-señal + migración 003.

### Gap 3: CLV no implementado
**Problema:** No existía job que capturara el precio de cierre antes del kickoff.
**Solución:** `clv_tracker.py` — APScheduler cada 5 min; captura `closing_price` y
`clv = closing_price - entry_price` cuando el kickoff está dentro de 90 min.

### Gap 4: Cierre pre-kickoff sin alineaciones
**Archivo:** `src/sporting_edge/agents/position_manager.py`
**Problema:** Force-close a 60 min sin validar si el edge sigue presente.
**Solución:** Two-stage close:
- Stage 1 (65 min): fetch lineups → `adjust_prediction_for_lineups()` → recalcular EV → cerrar si EV < threshold
- Stage 2 (30 min): force-close incondicional

### Gap 5: Settlement fuente única
**Archivo:** `src/sporting_edge/agents/bet_settler.py`
**Problema:** Solo consultaba API-Football.
**Solución:** `_reconcile_settlement()` cruza API-Football + Polymarket Gamma API;
`settlement_source` registra cuál fuente confirmó.

---

## 5. Modelo de datos — Cambios entregados

### Migración 003 — `signals` + `bets`
```sql
-- signals: CLOB prices at detection time
clob_bid, clob_ask, estimated_fill_price, book_liquidity_usd

-- bets: execution quality + CLV tracking
actual_fill_price, closing_price, clv
```

### Migración 004 — `bets`
```sql
settlement_source VARCHAR(20)  -- 'api_football' | 'polymarket' | 'both'
```

### Migración 005 — `bets`
```sql
lineup_checked BOOLEAN DEFAULT FALSE
```

---

## 6. Definición de Done (DoD)

- [x] Todos los issues del alcance cerrados (PRs #7, #9, #10, #11, #12)
- [x] Tests unitarios: 86 tests pasando (de 37 iniciales)
- [x] Migraciones SQL 003–005 creadas
- [x] ADR-006 actualizado a "Aceptado"
- [ ] README actualizado con nuevas variables de configuración ← pendiente
- [ ] Al menos 1 ciclo de paper trading completo con las nuevas métricas ← pendiente (go-live)

---

## 7. Historial de cambios

| Fecha | Cambio |
|-------|--------|
| 2026-06-04 | Documento inicial creado tras review arquitectónico |
| 2026-06-04 | EPIC completado — 5 issues entregados, 49 nuevos tests |
