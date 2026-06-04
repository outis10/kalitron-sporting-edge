# ADR-006: EV threshold diferenciado paper/live

**Fecha:** 2026-06-04
**Estado:** Propuesto

## Contexto

El threshold actual `MIN_EV_THRESHOLD=5%` aplica igual en paper trading y en
producción. En paper trading, señales con EV bajo solo desperdician ciclos de
análisis. En producción, ejecutar con solo 5% de EV no absorbe el error del modelo,
el slippage, el spread y las noticias inesperadas.

Análisis de componentes de coste real de una apuesta:
- Spread típico en mercados de fútbol: 2-4%
- Slippage estimado (FAK sobre book): 0.5-1.5%
- Error del modelo (estimado): 3-5%
- Total de "fricciones": ~6-10%

Con 5% de EV el bot estaría operando con EV neto negativo o cero en muchos casos.

## Decisión

**Propuesto:** Implementar dos thresholds en `config/settings.py`:

```python
min_ev_threshold: float = 0.05         # paper trading / backtesting
min_ev_threshold_live: float = 0.08    # producción (default conservador)
```

El `OddsAnalyzer` usa `settings.min_ev_threshold` en paper mode y
`settings.min_ev_threshold_live` cuando `not settings.paper_trading`.

Adicionalmente, para mercados con liquidez < $10k:
```python
min_ev_threshold_low_liquidity: float = 0.12
```

## Consecuencias

**Positivas:**
- En producción se filtra señales donde las fricciones consumirían el edge
- En paper trading se puede observar la distribución completa de señales

**Negativas:**
- Menos bets en producción (menor velocidad de acumulación de muestra)
- Tres parámetros en lugar de uno — más superficie de configuración

**Criterio de revisión:** Tras 3 meses de producción, ajustar `min_ev_threshold_live`
basado en el CLV promedio observado y el Brier Score del modelo.
