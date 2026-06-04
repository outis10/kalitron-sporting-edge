# ADR-004: Cliente de ejecución — CLOB V2

**Fecha:** 2026-06-04
**Estado:** Aceptado

## Contexto

Polymarket lanzó `py-clob-client-v2` en abril 2026 con una reducción de latencia
de señal-a-orden de 257ms (V1) a ~17ms (V2). El sistema arrancó con V1 durante
la fase de desarrollo para evitar migración prematura.

La migración a V2 fue bloqueada hasta decidir ir a producción. Al llegar ese punto,
se evaluó el costo de migración vs. el beneficio de latencia.

## Decisión

Migrar a **CLOB V2** (`py-clob-client-v2`) como única dependencia de ejecución.

Cambios aplicados en `src/sporting_edge/tools/polymarket_tools.py`:
- Reemplazar `py-clob-client` → `py-clob-client-v2` en `pyproject.toml`
- `get_clob_client()`: patrón de instancia única — un solo `ClobClient` con
  `signature_type` explícito, creds inyectadas vía `set_api_creds()` (sin bootstrap doble)
- `place_fak_order()`: usar `MarketOrderArgsV2` + `create_and_post_market_order()`
- `place_gtc_limit_order()`: usar `OrderArgsV2` + `create_and_post_order()`
- `get_usdc_balance()`: `BalanceAllowanceParams` con `signature_type` explícito

La migración de referencia está en `polymarket-trading-system/core/client_wrapper.py`.

## Consecuencias

**Positivas:**
- Latencia ~15x menor (17ms vs 257ms) — crítico para la ventana pre-kickoff
- `signature_type` explícito soporta EOA (0), Magic/email (1) y Gnosis Safe (2)

**Negativas:**
- V1 y V2 no son compatibles a nivel de firma de orden
- Órdenes V1 abiertas accesibles vía endpoint legacy durante la transición

**Pendiente:** Validar que USDC.e (no native USDC) sea el colateral en wallet
antes de habilitar `EXECUTE_TRADES=true`.
