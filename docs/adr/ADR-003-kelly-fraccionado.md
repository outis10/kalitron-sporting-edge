# ADR-003: Sizing — Quarter Kelly con cap duro

**Fecha:** 2026-04-01
**Estado:** Aceptado

## Contexto

Kelly Criterion completo maximiza el crecimiento logarítmico del bankroll pero
asume que el modelo es perfecto. En la práctica:
- El modelo tiene error de estimación
- Las probabilidades de Polymarket tienen spread
- Hay correlación entre partidos del mismo día (resultados no son independientes)
- Una racha de Kelly completo puede drawdown >50% antes de recuperarse

## Decisión

Usar **Quarter Kelly** (f* × 0.25) con dos caps adicionales:
1. `MAX_BET_PCT_BANKROLL = 2%` — nunca más del 2% del bankroll en una sola apuesta
2. `DAILY_LOSS_LIMIT_USD = $50` — detener ejecución si las pérdidas del día superan este umbral
3. `MAX_BETS_PER_LEAGUE_PER_DAY = 3` — limitar concentración por liga

Fórmula aplicada:
```
f = EV / odds_decimal  (Kelly completo)
stake = min(bankroll × f × 0.25, bankroll × MAX_BET_PCT_BANKROLL)
```

## Consecuencias

**Positivas:**
- Drawdowns máximos teóricos reducidos en ~75% vs Kelly completo
- El cap duro del 2% protege de estimaciones de EV muy optimistas
- Simple de auditar y explicar

**Negativas:**
- Crece más lento que Kelly completo en racha positiva
- El cap del 2% puede infraponderar señales con EV muy alto (>20%)

**Revisión prevista:** Una vez se acumule suficiente historia propia (>200 bets),
evaluar si subir a Half Kelly o ajustar el cap basado en el Sharpe observado.
