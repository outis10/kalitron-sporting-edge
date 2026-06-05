# SDD — EPIC-002: World Cup 2026 Integration

**Fecha:** 2026-06-05
**Estado:** Propuesto
**Autor:** outis10

---

## 1. Objetivo

Extender el pipeline para cubrir los 104 partidos del Mundial 2026 (FIFA World Cup,
`league=1`, `season=2026`) manteniendo compatibilidad total con las ligas de club
actualmente activas (EPL, La Liga, UCL).

El torneo arranca el **11 de junio de 2026** — el schedule ya está publicado en
API-Football y los mercados de Polymarket estarán disponibles a medida que se
acerquen las fechas.

---

## 2. Alcance

### Incluido

| # | Issue | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 1 | #14 | Per-league season config | Alta |
| 2 | #15 | Activar league_id=1 + validar matching Polymarket | Alta |
| 3 | #16 | Neutral venue — eliminar home advantage en torneos internacionales | Media |
| 4 | #17 | Endpoint `/standings` para contexto de fase de grupos | Media |

### Fuera de alcance (EPIC futuro)

- Modelo de forma basado en stats de eliminatorias previas al torneo
- Dashboard de grupos / bracket knockout
- Predicción de campeón a largo plazo (outright markets)
- Integración de ratings FIFA/ELO como prior del modelo

---

## 3. Decisiones arquitectónicas relacionadas

| ADR | Título | Estado |
|-----|--------|--------|
| [ADR-001](../adr/ADR-001-modelo-predictivo-dixon-coles.md) | Modelo predictivo Dixon-Coles | Aceptado |
| [ADR-007](../adr/ADR-007-neutral-venue-international-tournaments.md) | Neutral venue en torneos internacionales | Propuesto |

---

## 4. Contexto técnico

### 4.1 API-Football — identificadores clave

```
league_id = 1
season    = 2026
```

Cobertura confirmada para `league=1&season=2026`:
- `fixtures.events`, `fixtures.lineups`, `fixtures.statistics` → `true`
- `standings`, `players`, `injuries`, `predictions`, `odds` → `true`

Schedule completo (104 partidos) ya disponible. Los partidos se añaden
progresivamente conforme avanza el torneo.

### 4.2 Problema de season hardcodeada

`data_collector.py:44` tiene:
```python
CURRENT_SEASON = 2024  # free-tier workaround
```

Este valor se usa indiscriminadamente para todas las ligas. Para `league_id=1`
necesitamos `season=2026`. La solución es un mapeo por liga sin tocar la lógica
de date-shifting que usan las ligas de club.

### 4.3 Problema de venue neutral

El modelo Dixon-Coles aplica **ventaja de local** (home advantage factor) por defecto.
En el Mundial todos los partidos son en sede neutral — no hay equipo local real.
API-Football asigna home/away basándose en el orden de sorteo, no en la sede.
Aplicar home advantage aquí introduce sesgo sistemático en las probabilidades.

### 4.4 Matching de mercados Polymarket

`GammaClient.find_match_markets()` busca:
```python
f"{home_team} vs {away_team}"   # ej: "Brazil vs Argentina"
f"{home_team} {away_team}"
home_team                        # ej: "Brazil"
```

Para selecciones esto debería funcionar, pero los mercados del Mundial pueden
estar titulados como _"Will Brazil win their World Cup group?"_ o
_"Argentina vs France 2026 World Cup"_. Hay que validar con datos reales
antes de dar por bueno el matching.

### 4.5 Datos escasos en Jornada 1

`get_team_statistics(league=1, season=2026)` en la primera jornada devuelve
`matches_played=0` — no hay stats de la temporada aún. El modelo ya maneja esto
con weight scaling (`w = matches_played / 3`), lo que resulta en confidencia
≈ 0.45-0.50. Esto es correcto desde el punto de vista de gestión de riesgo:
no apostar fuerte sin datos. No requiere cambio de código para v1.

---

## 5. Diseño por issue

### Issue #14 — Per-league season config

**Archivo principal:** `src/sporting_edge/agents/data_collector.py`

Reemplazar la constante plana:
```python
CURRENT_SEASON = 2024
```

Por un mapeo explícito con override por liga:
```python
# Default season for club leagues (free-tier covers up to 2024)
DEFAULT_SEASON = 2024

# Per-league season overrides — add here when a new competition uses a different year
SEASON_OVERRIDE: dict[int, int] = {
    1: 2026,  # FIFA World Cup 2026
}

def _season_for(league_id: int) -> int:
    return SEASON_OVERRIDE.get(league_id, DEFAULT_SEASON)
```

El date-shifting block permanece intacto para las ligas de club
(`DEFAULT_SEASON < today.year - 1` sigue siendo cierto). Para `league_id=1`
el season=2026 es real, así que no entra en el bloque de shifting.

**Settings (`config/settings.py`):** Sin cambios de código — el override vive
en `data_collector.py` junto al comentario `FIXME(paid-api)` existente.

### Issue #15 — Activar league_id=1 + validar Polymarket

**Archivo:** `src/sporting_edge/config/settings.py`
```python
active_leagues: str = "39,140,2,1"  # EPL, La Liga, UCL, World Cup
```

**Validación Polymarket:** Ejecutar manualmente:
```python
gamma = GammaClient()
markets = await gamma.find_match_markets("Brazil", "Mexico")
print([m["question"] for m in markets])
```

Si el matching no retorna mercados relevantes, ajustar las queries en
`find_match_markets()` para incluir variantes del tipo
`"FIFA World Cup"`, `"WC2026"`.

**.env.example:** Documentar que `ACTIVE_LEAGUES=1` activa el Mundial.

### Issue #16 — Neutral venue

**Archivo:** `src/sporting_edge/agents/model_predictor.py`

Añadir constante con leagues de sede neutral:
```python
NEUTRAL_VENUE_LEAGUES: frozenset[int] = frozenset({
    1,   # FIFA World Cup
    4,   # Euro Championship
    5,   # UEFA Nations League (final four)
})
```

En `_compute_scoring_rates()` (o donde se aplica home advantage),
detectar si la liga es neutral y pasar `home_advantage=1.0` (sin boost):
```python
is_neutral = match.league.id in NEUTRAL_VENUE_LEAGUES
home_adv = 1.0 if is_neutral else HOME_ADVANTAGE_FACTOR
```

Registrar en el log de predicción: `venue_type="neutral"`.

**ADR-007** documenta la decisión y la lista de ligas neutrales.

### Issue #17 — Endpoint `/standings`

**Archivo:** `src/sporting_edge/tools/football_api.py`

Nuevo método:
```python
async def get_standings(self, league_id: int, season: int) -> list[GroupStanding]:
    data = await self._get("/standings", {"league": league_id, "season": season})
    ...
```

**Schema nuevo** (`models/schemas.py`):
```python
class GroupStanding(BaseModel):
    group: str          # "Group A", "Group B", ...
    rank: int
    team_id: int
    team_name: str
    points: int
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_diff: int
    form: str | None    # "WWDL..." últimos resultados
```

Para v1, el uso es informativo: enriquecer el log del `data_collector_node`
con la posición del grupo. La integración como feature del modelo queda
para un EPIC posterior.

---

## 6. Modelo de datos — Cambios

No se requieren migraciones SQL para este EPIC. Todos los cambios son en
capa de configuración y lógica de negocio. `league_id=1` ya es un valor
válido en la tabla `leagues` (FK sin restricción de dominio).

---

## 7. Definición de Done (DoD)

- [ ] Issues #14–#17 cerrados
- [ ] `ACTIVE_LEAGUES=39,140,2,1` en `.env.example`
- [ ] Tests unitarios actualizados: `_season_for()`, neutral venue flag
- [ ] Pipeline completo ejecutado en dry-run con `league_id=1, season=2026`
  y al menos 1 fixture retornado desde API-Football
- [ ] Validación Polymarket documentada en issue #15 (positiva o con workaround)
- [ ] ADR-007 actualizado a "Aceptado"

---

## 8. Historial de cambios

| Fecha | Cambio |
|-------|--------|
| 2026-06-05 | Documento inicial — análisis pre-implementación |
