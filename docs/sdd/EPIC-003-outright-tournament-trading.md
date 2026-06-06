# SDD — EPIC-003: Outright Tournament Trading

**Fecha:** 2026-06-05
**Estado:** Propuesto
**Autor:** outis10

---

## 1. Objetivo

Extender el sistema para operar mercados outright de torneo en Polymarket
("Will X win the 2026 FIFA World Cup?") con una estrategia de price catalyst
trading: entrar cuando el mercado subestima a un equipo, salir con take-profit
después de resultados positivos.

No se requiere predecir el campeón — solo detectar precios incorrectos y
reaccionar más rápido que el mercado a los resultados de cada partido.

---

## 2. Contexto de mercado

- **48 mercados activos** con $336M liquidez total y $49M/día de volumen
- Estructura: CLOB con NegRisk (todos los YES tokens del mismo pool)
- Fees: 3% taker → necesita >6% de movimiento para break-even en round-trip
- Datos clave: `clobTokenIds`, `outcomePrices`, `bestBid`, `bestAsk` via Gamma API
- **NO** requiere esperar resolución — posiciones se cierran vía CLOB como cualquier token

Ver [ADR-008](../adr/ADR-008-outright-tournament-trading.md) para decisión arquitectónica.

---

## 3. Alcance

### Incluido

| # | Issue | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 1 | [#28](https://github.com/outis10/kalitron-sporting-edge/issues/28) | `bet_type` field en BetORM + migración | Alta |
| 2 | [#29](https://github.com/outis10/kalitron-sporting-edge/issues/29) | `OutrightCollector` — discovery y parsing de mercados "Will X win WC?" | Alta |
| 3 | [#30](https://github.com/outis10/kalitron-sporting-edge/issues/30) | `OutrightAnalyzer` — señal de entrada proactiva (EV vs modelo) | Alta |
| 4 | [#31](https://github.com/outis10/kalitron-sporting-edge/issues/31) | Pipeline outright + integración con ExecutionAgent/PositionManager | Media |
| 5 | [#32](https://github.com/outis10/kalitron-sporting-edge/issues/32) | Settings: EV threshold, TP/SL multipliers, max positions | Media |
| 6 | [#33](https://github.com/outis10/kalitron-sporting-edge/issues/33) | Outright shock detector — entrada reactiva en caídas fuertes | Media |

### Dos tipos de señal de entrada

```
Señal proactiva (OutrightAnalyzer):        Señal reactiva (ShockDetector):
Modelo → p_equipo > p_mercado + fee        PolymarketStreamer detecta caída ≥15%
→ BUY antes de que el mercado corrija       → BUY en el fondo de la caída
                                            → válido solo si modelo sigue diciendo EV > threshold
```

### Fuera de alcance

- Simulador Monte Carlo completo del torneo
- In-play trading (durante los 90 minutos del partido)
- Datos históricos de clasificatorias para calibración del modelo
- Dashboard de posiciones outright
- Bracket/knockout tracking automatizado

---

## 4. Arquitectura

```
Pipeline Match (sin cambios):
DataCollector → ModelPredictor(1X2) → OddsAnalyzer → RiskManager → ExecutionAgent

Pipeline Outright (nuevo, comparte infraestructura):
OutrightCollector → OutrightAnalyzer → RiskManager → ExecutionAgent
                                           ↑ reuse      ↑ reuse
```

### 4.1 Reutilización del stack existente

| Componente | Acción |
|---|---|
| `RiskManager` | Sin cambios — Kelly sizing aplica igual |
| `ExecutionAgent` | Ajuste menor: leer `clobTokenIds[0]` para token YES |
| `PositionManager` | Sin cambios — `kickoff_utc=None` → price_check (TP/SL) |
| `BetSettler` | Skip para `bet_type='outright'` (Polymarket resuelve solo) |
| `CLVTracker` | Skip para `bet_type='outright'` |

### 4.2 Componentes nuevos

**`OutrightCollector`** (`agents/outright_collector.py`):
- Llama `GET /gamma-api/markets?active=true` con filtro `"will X win the 2026 FIFA world cup"`
- Parsea `clobTokenIds`, `outcomePrices`, `bestBid`, `bestAsk`
- Agrupa por torneo (event slug)
- Devuelve lista de `OutrightMarket` (nuevo schema)

**`OutrightAnalyzer`** (`agents/outright_analyzer.py`):
- Compara precio actual del mercado vs estimación propia
- Estimación: distribución de Dirichlet sobre todos los equipos usando rankings FIFA como prior
- Señal si `EV = (p_modelo / p_market) - 1 >= OUTRIGHT_EV_THRESHOLD`

---

## 5. Modelo de datos

### Nuevo campo en `BetORM`
```sql
ALTER TABLE bets ADD COLUMN bet_type VARCHAR(20) DEFAULT 'match';
-- 'match' | 'outright'
```

### Nuevos schemas (`models/schemas.py`)
```python
class OutrightMarket(BaseModel):
    condition_id: str
    question: str
    team_name: str
    yes_token_id: str
    no_token_id: str
    yes_price: float       # current market price (from outcomePrices or bestAsk)
    best_bid: float
    best_ask: float
    liquidity: float
    volume_24h: float
    neg_risk_market_id: str  # shared pool ID

class OutrightSignal(BaseModel):
    market: OutrightMarket
    model_probability: float
    market_probability: float
    expected_value: float
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
```

---

## 6. Settings nuevos

```python
# Outright tournament trading
outright_ev_threshold: float = 0.15      # higher than match (compensates 3% ×2 fee)
outright_tp_multiplier: float = 2.5      # close at 2.5× entry price
outright_sl_multiplier: float = 0.5      # stop-loss at 50% of entry
outright_max_positions: int = 3          # max simultaneous outright positions
outright_max_bet_pct: float = 0.01       # 1% bankroll per outright (more conservative)
```

---

## 7. Definición de Done (DoD)

- [ ] Issues #28–#33 cerrados
- [ ] Pipeline outright ejecutado en dry-run con mercados WC reales
- [ ] `bet_type='outright'` visible en DBeaver con posiciones separadas
- [ ] PositionManager hace TP/SL para outright sin kickoff
- [ ] ADR-008 actualizado a "Aceptado"
- [ ] Al menos 1 señal outright generada en paper trading

---

## 8. Historial de cambios

| Fecha | Cambio |
|-------|--------|
| 2026-06-05 | Documento inicial — diseño pre-implementación |
