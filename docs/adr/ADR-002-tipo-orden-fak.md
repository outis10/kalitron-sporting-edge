# ADR-002: Tipo de orden — FAK sobre GTC

**Fecha:** 2026-04-01
**Estado:** Aceptado

## Contexto

En mercados de predicción deportiva, el edge tiene una ventana temporal muy corta:
- Las odds reflejan nueva información (lesiones, alineaciones, clima) casi en tiempo real
- Una orden GTC (Good-Till-Cancelled) colocada a las 10am puede ejecutarse a las 11:59am
  cuando el edge ya desapareció o incluso se invirtió
- El slippage en mercados con bajo volumen puede ser significativo

## Decisión

Usar exclusivamente **FAK (Fill-and-Kill)** para la ejecución de señales.

FAK llena lo que puede al mejor precio disponible en el momento de la orden
y cancela el resto inmediatamente. Nunca deja órdenes abiertas en el libro.

Adicionalmente, se usa `hint_price` (el best_ask pre-fetched del orderbook)
para reducir el round-trip interno del CLOB y estrechar la ventana de race
condition con market makers.

## Consecuencias

**Positivas:**
- Sin riesgo de ejecución tardía fuera de la ventana de edge
- No hay órdenes huérfanas que limpiar
- `hint_price` reduce latencia efectiva

**Negativas:**
- Fill parcial posible si no hay liquidez suficiente para el notional completo
- Precio de fill puede ser ligeramente peor que el best_ask si la liquidez
  del primer nivel es insuficiente (`estimate_fill()` lo detecta antes de ejecutar)
- No deja liquidez como market maker (sin rebate de fees)

**GTC se mantiene** como función disponible (`place_gtc_limit_order`) para uso
futuro en estrategias de provisión de liquidez, pero no se usa en el pipeline principal.
