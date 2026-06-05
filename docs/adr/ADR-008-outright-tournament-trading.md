# ADR-008: Estrategia de trading en mercados outright de torneo

**Fecha:** 2026-06-05
**Estado:** Propuesto

## Contexto

Polymarket tiene 48 mercados activos "Will X win the 2026 FIFA World Cup?" con
$336M de liquidez total y $49M de volumen diario. Estos mercados son CLOB (order book)
— los YES tokens se pueden comprar y vender en cualquier momento sin esperar resolución.

La estrategia "enter and exit quickly" no requiere acertar el campeón del torneo.
Requiere:
1. Identificar equipos cuyo precio de mercado es inferior a la estimación del modelo
2. Comprar YES tokens
3. Después de que el equipo gane un partido, el precio sube
4. Vender con ganancia antes de que el torneo termine

Esta es una estrategia de **price catalyst trading**, no de predicción de campeón.

## Diferencias vs mercados match 1X2

| Aspecto | Match 1X2 | Outright torneo |
|---|---|---|
| Resolución | 90 min (fija) | Indefinida (equipo eliminado o campeón) |
| Price catalyst | Resultado del partido | Cada partido del torneo |
| Exit signal | Kickoff + TP/SL | Solo TP/SL (precio) |
| Modelo de entrada | Dixon-Coles 1X2 | Fuerza relativa vs campo |
| Fees CLOB | ~1-2% spread | 3% taker fee |

## Decisión

Implementar un pipeline paralelo para outright markets que reutiliza el stack
de ejecución existente (RiskManager, ExecutionAgent, PositionManager).

**Modelo de entrada (OutrightAnalyzer):**
No se usa simulación Monte Carlo completa. Se usa un estimador de fuerza
relativa basado en:
- Precio actual del mercado para todos los equipos del mismo grupo
- Ranking FIFA como prior de fuerza
- Ajuste dinámico por resultados observados en la temporada 2026

**Señal de entrada:**
```
EV = (p_modelo / p_mercado) - 1 >= OUTRIGHT_EV_THRESHOLD (default 15%)
```
Threshold más alto que match markets (8%) para compensar el 3% de fee (×2) y
la mayor incertidumbre del modelo.

**Señales de salida:**
- Take-profit: `current_price >= entry_price × OUTRIGHT_TP_MULTIPLIER` (default 2.5×)
- Stop-loss: `current_price <= entry_price × OUTRIGHT_SL_MULTIPLIER` (default 0.5×)
- Team eliminado: Polymarket resuelve automáticamente a $0 → pérdida total

**Gestión de riesgo adicional:**
- Max 1 posición outright activa por torneo (correlación alta entre equipos)
- `bet_type='outright'` en BetORM para separar métricas de match markets

## Alternativas descartadas

**A. Simulador Monte Carlo completo**
Requiere datos de clasificatorias de múltiples ligas, manejo del bracket 48 equipos,
y calibración de parámetros. Complejidad desproporcionada para v1.

**B. Esperar solo mercados per-match**
Los mercados por partido tendrán alta liquidez y el pipeline existente los maneja.
Los outright son complementarios — no excluyentes.

**C. Operar ambos en el mismo pipeline**
Mezclar lógica de match y outright en los mismos agentes aumenta complejidad.
Pipelines separados con infraestructura compartida es más limpio.

## Consecuencias

- Stack de ejecución (CLOB, FAK orders, PositionManager) reutilizado sin cambios
- Campo `bet_type` en BetORM para diferenciar posiciones en reporting
- OutrightAnalyzer puede operar sin datos de partido previos (precio de mercado como señal primaria)
- Fee del 3% (×2 por round-trip) requiere mínimo 6% de movimiento de precio para break-even
