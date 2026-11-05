# Tiki Data — El fútbol en datos

App de información de fútbol desarrollada por Teo Gunther Crespo.

Datos de football-data.org (ligas de Europa, Brasileirão y Mundial), Highlightly (Liga Profesional,
Copa Argentina, Libertadores y Sudamericana), TheSportsDB (estadios, camisetas y fotos) y titulares de
portales deportivos.

## Publicar en Render
- Tipo: Web Service · Plan: Free
- Build command: `pip install -r requirements.txt`
- Start command: `python app.py`

## Variables de entorno (Render → tikidata → Environment)
| Nombre | Qué es | ¿Obligatoria? |
|---|---|---|
| `FOOTBALL_DATA_KEY` | Clave de football-data.org | Sí |
| `HIGHLIGHTLY_KEY` | Clave de Highlightly (plan gratis: 100 pedidos por día) | Para los torneos argentinos y sudamericanos |
| `HL_IDS` | Números fijos de los torneos, para no gastar pedidos: `LPF=109712,CAR=111414,LIB=11847,SUD=10145` | Recomendada |
| `YOUTUBE_API_KEY` | Clave de la API de YouTube, para mostrar la portada del resumen de cada partido | Opcional |

Las claves van solo en Render. Nunca se escriben en el código ni se suben a GitHub.

## Ver el estado de Highlightly
`/api/hl_estado` muestra los pedidos usados en el día, cada cuántos minutos se actualiza cada cosa
(prioridad: Boca → River → resto de la Liga Profesional → copas) y los torneos encontrados.

## Probar en la compu
```
set FOOTBALL_DATA_KEY=tu_clave
py app.py
```
