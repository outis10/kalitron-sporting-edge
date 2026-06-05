# ADR-007: Neutral venue en torneos internacionales

**Fecha:** 2026-06-05
**Estado:** Aceptado

## Contexto

El modelo Dixon-Coles incluye un factor de **home advantage** que incrementa
λ_home (tasa de goles esperada del equipo local). Este factor está calibrado
sobre datos de ligas de club donde el equipo local juega en su estadio.

En torneos internacionales como el Mundial o la Eurocopa, los partidos se juegan
en sedes neutrales. API-Football asigna los roles "home" y "away" basándose en
el orden de sorteo del fixture, no en ninguna ventaja geográfica real.

Aplicar home advantage en estos contextos introduce sesgo sistemático:
el equipo listado como "home" recibe probabilidades infladas artificialmente.

## Decisión

Mantener el factor `HOME_ADVANTAGE_FACTOR` en su valor actual para todas las
ligas de club. Para un conjunto explícito de ligas de sede neutral, aplicar
`home_advantage = 1.0` (sin modificación de λ).

La lista de ligas neutrales se define como constante en `model_predictor.py`:

```python
NEUTRAL_VENUE_LEAGUES: frozenset[int] = frozenset({
    1,   # FIFA World Cup
    4,   # UEFA European Championship
    5,   # UEFA Nations League (final four)
})
```

Se agrega un campo `venue_type` ("neutral" | "home") al log de cada predicción
para facilitar análisis posterior de calibración del modelo.

## Alternativas descartadas

**A. Factor separado por torneo calibrado sobre datos históricos**
Requiere suficiente muestra de resultados de cada torneo con el modelo actual
(años de datos). No viable con el volumen actual.

**B. Ignorar el problema para v1**
Introduce sesgo conocido y medible. Si los mercados de Polymarket están
correctamente calibrados, el sesgo del modelo genera señales con EV inflado
en el equipo "home", lo que puede resultar en pérdidas sistemáticas.

**C. Lista dinámica desde API-Football coverage**
Más flexible pero introduce una llamada extra a la API y complejidad innecesaria.
La lista estática es suficiente para los torneos que cubre el sistema actualmente.

## Consecuencias

- Probabilidades 1X2 más calibradas en partidos del Mundial.
- El campo `venue_type` en los logs permite auditar si la corrección reduce
  el error de calibración conforme se acumulen resultados.
- La lista `NEUTRAL_VENUE_LEAGUES` debe mantenerse actualizada si se añaden
  nuevas competencias internacionales.
