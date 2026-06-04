# ADR-001: Modelo predictivo — Dixon-Coles Poisson

**Fecha:** 2026-04-01
**Estado:** Aceptado

## Contexto

Para calcular probabilidades 1X2 necesitamos un modelo que sea:
- Explicable (poder auditar por qué generó un signal)
- Backtestable sobre datos históricos
- Rápido (debe correr en el pipeline sin latencia perceptible)
- Ajustable conforme se acumule data propia

Las alternativas evaluadas fueron:
1. Modelo Poisson simple (sin corrección de bajos marcadores)
2. Dixon-Coles Poisson con corrección ρ para {0-0, 1-0, 0-1, 1-1}
3. Gradient Boosting sobre features tabulares
4. APIs de probabilidades externas (Betfair, Pinnacle odds como proxy)

## Decisión

Usar **Dixon-Coles Poisson** (opción 2) implementado en NumPy/SciPy puro.

El modelo estima λ_home y λ_away a partir de la forma reciente de cada equipo
relativizada al promedio de la liga, aplica ventaja de local, y corrige los
marcadores de baja puntuación con el parámetro ρ = -0.13 (estimación estándar
de la industria). Se blend con H2H histórico cuando hay ≥5 partidos de muestra.

## Consecuencias

**Positivas:**
- Sin dependencias de ML (no requiere GPU, no hay overfitting por ahora)
- El score de confianza penaliza automáticamente cuando hay poca data
- Fácil de auditar: `factors_used` en cada predicción muestra exactamente qué inputs usó
- Backtestable: mismo código corre en `backtesting/engine.py`

**Negativas / limitaciones conocidas:**
- No incorpora lesiones, suspensiones ni alineaciones confirmadas
- Los priors de liga (`avg_home_goals`, `avg_away_goals`) son globales, no por liga
- xG solo da un bonus de confianza, no influye directamente en λ todavía
- Sin motivación competitiva (equipos ya clasificados o relegados pueden relajarse)

**Pendiente (ver EPIC-001):**
- Integrar alineaciones confirmadas (API-Football `/fixtures/lineups`) para ajustar λ
- Priors por liga en lugar de globales
- Reemplazar o complementar con xG para λ cuando haya suficiente data propia
