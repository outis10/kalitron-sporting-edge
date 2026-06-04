# ADR-005: CLV como métrica primaria de validación de edge

**Fecha:** 2026-06-04
**Estado:** Aceptado

## Contexto

Para validar que el bot tiene edge real necesitamos una métrica que no requiera
miles de resultados para ser estadísticamente significativa. Las opciones son:

1. **Win rate / ROI** — requiere >500 bets para significancia; muy ruidoso a corto plazo
2. **Brier Score** — mide calibración del modelo, no rentabilidad
3. **Closing Line Value (CLV)** — mide si el precio de entrada es mejor que el precio
   de cierre del mercado al kickoff

CLV = `precio_cierre_kickoff - precio_entrada`

Si el bot compra consistentemente más barato que el precio al que el mercado
converge en el kickoff, significa que el modelo detecta información que el mercado
tardará en incorporar — evidencia de edge real.

## Decisión

Adoptar **CLV como métrica primaria** de validación de edge durante la fase
de paper trading y los primeros meses de producción.

Implementación requerida (ver EPIC-001, Issue #3):
1. Guardar `entry_price`, `entry_bid`, `entry_ask` en cada signal/bet
2. Job programado: ~10 min antes del kickoff, registrar `closing_price` del CLOB
3. Calcular `clv = closing_price - entry_price` (positivo = entramos más barato)
4. Dashboard/reporte: CLV promedio por liga, por outcome, por nivel de EV

## Consecuencias

**Positivas:**
- Detecta edge real con ~50-100 bets (vs 500+ con ROI)
- Independiente del resultado del partido (ruido aleatorio)
- Validación en tiempo casi real: sabemos si hay edge antes de acumular resultados

**Negativas:**
- Requiere infraestructura adicional (job de closing price)
- CLV positivo es condición necesaria pero no suficiente — aún necesitamos
  confirmar que el edge sobrevive al slippage y al spread
- Mercados muy ilíquidos pueden tener cierre ruidoso

**Métrica secundaria:** Brier Score del modelo (calibración de probabilidades)
para detectar si el modelo está sobre/infraestimando sistemáticamente.
