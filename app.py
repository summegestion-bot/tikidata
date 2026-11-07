"""
TIKI DATA v2 — El fútbol en datos
Versión con el diseño formal y datos reales de football-data.org.

En internet (Render): comando de inicio "python app.py" y la clave en Environment.

En la compu (ventana negra, dentro de la carpeta):
    set FOOTBALL_DATA_KEY=tu_clave
    py app.py
Para verla también en el celular (mismo WiFi):
    py app.py celular
Para apagarlo: Ctrl + C en la ventana negra.

Si en la carpeta está el archivo "tikidata_escudo.png", se usa como logo.
"""

import json
import os
import socket
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import deque
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import re

import requests

BASE_URL = "https://api.football-data.org/v4"
PUERTO = 8000
ARG = timezone(timedelta(hours=-3))
CARPETA = os.path.dirname(os.path.abspath(__file__))
CLAVE = os.environ.get("FOOTBALL_DATA_KEY", "").strip()

# Ligas incluidas en el plan gratuito (código: nombre, región)
LIGAS = {
    "CL": ("Champions League", "Europa"),
    "PL": ("Premier League", "Inglaterra"),
    "PD": ("La Liga", "España"),
    "SA": ("Serie A", "Italia"),
    "BL1": ("Bundesliga", "Alemania"),
    "FL1": ("Ligue 1", "Francia"),
    "PPL": ("Primeira Liga", "Portugal"),
    "DED": ("Eredivisie", "Países Bajos"),
    "ELC": ("Championship", "Inglaterra"),
    "BSA": ("Brasileirão", "Brasil"),
    "WC": ("Mundial 2026", "Selecciones"),
}

# Noticias: portales que publican sus titulares en formato RSS.
# Si alguno no responde, se saltea solo. Se pueden agregar o sacar.
NOTICIAS = [
    ("OLÉ", "https://www.ole.com.ar/rss/ultimas-noticias/"),
    ("OLÉ", "https://www.ole.com.ar/rss/futbol-primera/"),
    ("TYC SPORTS", "https://www.tycsports.com/rss.xml"),
    ("TYC SPORTS", "https://www.tycsports.com/rss/futbol-argentino.xml"),
    ("TYC SPORTS", "https://www.tycsports.com/rss/ultimas-noticias.xml"),
    ("ESPN", "https://www.espn.com.ar/espn/rss/futbol/news"),
    ("ESPN", "https://www.espn.com.ar/espn/rss/news"),
    ("ESPN", "https://espndeportes.espn.com/espn/rss/futbol/news"),
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/futbol-internacional.xml"),
]
# Solo noticias de estos portales (el nombre que se muestra y su sitio web)
SITIOS = {"OLÉ": "ole.com.ar", "TYC SPORTS": "tycsports.com", "ESPN": "espn.com.ar", "MARCA": "marca.com"}
# Búsquedas en Google Noticias (Argentina), limitadas a esos portales.
# La foto se consigue después entrando a la nota.
_ARG = '(Boca OR River OR "Liga Profesional" OR Racing OR Independiente OR "San Lorenzo" OR "Selección Argentina")'
BUSQUEDAS = (
    [_ARG + " site:" + s for s in SITIOS.values()]
    + ['("Primera Nacional" OR ascenso) fútbol site:' + s for s in ("ole.com.ar", "tycsports.com")]
    + ["fútbol site:" + s for s in SITIOS.values()]
)
NAVEGADOR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# ---------------------------------------------------------------------------
# Pedidos a la API, con memoria y respetando el límite de 10 pedidos por minuto
# ---------------------------------------------------------------------------
_memoria = {}
_turnos = deque()
_candado = threading.Lock()


def esperar_turno():
    with _candado:
        while True:
            ahora = time.time()
            while _turnos and ahora - _turnos[0] > 60:
                _turnos.popleft()
            if len(_turnos) < 9:
                _turnos.append(ahora)
                return
            time.sleep(61 - (ahora - _turnos[0]))


def api(ruta, ttl=120):
    ahora = time.time()
    if ruta in _memoria and ahora - _memoria[ruta][0] < ttl:
        return _memoria[ruta][1]
    for intento in range(2):
        esperar_turno()
        r = requests.get(BASE_URL + ruta, headers={"X-Auth-Token": CLAVE}, timeout=20)
        if r.status_code == 429 and intento == 0:
            time.sleep(30)
            continue
        break
    if r.status_code == 403:
        raise RuntimeError("Ese dato no está incluido en el plan gratuito de la API.")
    if r.status_code != 200:
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = ""
        raise RuntimeError(f"La API respondió {r.status_code}. {msg}")
    datos = r.json()
    _memoria[ruta] = (ahora, datos)
    return datos


# ---------------------------------------------------------------------------
# Convertir los datos de la API a un formato simple para la página
# ---------------------------------------------------------------------------
def a_hora_arg(utc):
    return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(ARG)


def equipo(t):
    t = t or {}
    return {
        "id": t.get("id"),
        "nombre": t.get("shortName") or t.get("name") or "A definir",
        "escudo": t.get("crest") or "",
        "sigla": t.get("tla") or "",
    }


def partido(p, liga_defecto=None):
    dt = a_hora_arg(p["utcDate"])
    estado = p.get("status", "")
    goles = (p.get("score") or {}).get("fullTime") or {}
    comp = p.get("competition") or {}
    codigo = comp.get("code") or liga_defecto
    return {
        "id": p.get("id"),
        "fecha": dt.strftime("%Y-%m-%d"),
        "hora": dt.strftime("%H:%M"),
        "estado": estado,
        "vivo": estado in ("IN_PLAY", "PAUSED"),
        "terminado": estado in ("FINISHED", "AWARDED"),
        "minuto": p.get("minute"),
        "jornada": p.get("matchday"),
        "fase": p.get("stage"),
        "liga": codigo,
        "ligaNombre": LIGAS.get(codigo, (comp.get("name") or codigo or "", ""))[0],
        "ligaEmblema": comp.get("emblem") or "",
        "local": equipo(p.get("homeTeam")),
        "visitante": equipo(p.get("awayTeam")),
        "gl": goles.get("home"),
        "gv": goles.get("away"),
    }


def datos_hoy(fecha):
    grupos_hl = []
    if HL_KEY:
        for cod in HL_LIGAS:
            try:
                ps = sorted(hl_hoy(cod, fecha), key=lambda p: p["hora"])
            except Exception:
                continue
            if ps:
                grupos_hl.append({"liga": cod, "nombre": HL_LIGAS[cod][0], "region": HL_LIGAS[cod][1],
                                  "emblema": ps[0]["ligaEmblema"], "partidos": [{k: v for k, v in p.items() if k != "_inicio"} for p in ps]})
    res = datos_hoy_fd(fecha)
    res["grupos"] = grupos_hl + res["grupos"]
    return res


def datos_hoy_fd(fecha):
    d = date.fromisoformat(fecha)
    hasta = (d + timedelta(days=1)).isoformat()
    datos = api(f"/matches?dateFrom={fecha}&dateTo={hasta}", ttl=60)
    lista = [partido(p) for p in datos.get("matches", [])]
    lista = [p for p in lista if p["fecha"] == fecha and p["liga"] in LIGAS]
    lista.sort(key=lambda p: (list(LIGAS).index(p["liga"]), p["hora"]))
    grupos = []
    for p in lista:
        if not grupos or grupos[-1]["liga"] != p["liga"]:
            grupos.append({"liga": p["liga"], "nombre": p["ligaNombre"],
                           "region": LIGAS[p["liga"]][1], "emblema": p["ligaEmblema"], "partidos": []})
        grupos[-1]["partidos"].append(p)
    return {"fecha": fecha, "grupos": grupos}


def datos_calendario(liga):
    if liga in HL_LIGAS:
        ps = hl_temporada(liga)
        return {"liga": liga, "nombre": HL_LIGAS[liga][0], "emblema": ps[0]["ligaEmblema"] if ps else "",
                "partidos": [{k: v for k, v in p.items() if k != "_inicio"} for p in ps]}
    datos = api(f"/competitions/{liga}/matches", ttl=1800)
    comp = datos.get("competition") or {}
    lista = [partido(p, liga) for p in datos.get("matches", [])]
    for p in lista:
        p["ligaEmblema"] = p["ligaEmblema"] or comp.get("emblem", "")
    return {"liga": liga, "nombre": LIGAS[liga][0], "emblema": comp.get("emblem", ""), "partidos": lista}


def datos_partido(id_partido):
    if str(id_partido).startswith("h"):
        return datos_partido_hl(str(id_partido)[1:])
    p = api(f"/matches/{id_partido}", ttl=60)
    res = partido(p)
    medio = (p.get("score") or {}).get("halfTime") or {}
    res["medio"] = [medio.get("home"), medio.get("away")]
    res["estadio"] = p.get("venue") or ""
    arbitros = p.get("referees") or []
    res["arbitro"] = arbitros[0].get("name") if arbitros else ""
    res["goles"] = [{
        "minuto": g.get("minute"),
        "jugador": (g.get("scorer") or {}).get("name", ""),
        "equipo": (g.get("team") or {}).get("id"),
        "tipo": g.get("type", ""),
    } for g in (p.get("goals") or [])]
    try:
        h = api(f"/matches/{id_partido}/head2head?limit=6", ttl=3600)
        ag = h.get("aggregates") or {}
        res["historial"] = {
            "total": ag.get("numberOfMatches"),
            "local": (ag.get("homeTeam") or {}).get("wins"),
            "empates": (ag.get("homeTeam") or {}).get("draws"),
            "visitante": (ag.get("awayTeam") or {}).get("wins"),
            "partidos": [partido(x) for x in h.get("matches", []) if x.get("id") != res["id"]],
        }
    except Exception:
        res["historial"] = None
    return res


def datos_tabla(liga):
    if liga in HL_LIGAS:
        return hl_tabla(liga)
    datos = api(f"/competitions/{liga}/standings", ttl=600)
    grupos = []
    for t in datos.get("standings", []):
        if t.get("type") != "TOTAL":
            continue
        filas = [{
            "pos": f["position"], "equipo": equipo(f["team"]),
            "pj": f["playedGames"], "g": f["won"], "e": f["draw"], "p": f["lost"],
            "dg": f["goalDifference"], "pts": f["points"], "forma": f.get("form") or "",
        } for f in t.get("table", [])]
        nombre = (t.get("group") or "").replace("GROUP_", "Grupo ").replace("_", " ").title()
        grupos.append({"nombre": nombre, "filas": filas})
    return {"liga": liga, "nombre": LIGAS[liga][0], "grupos": grupos}


def datos_goleadores(liga):
    if liga in HL_LIGAS:
        return {"liga": liga, "nombre": HL_LIGAS[liga][0], "goleadores": []}
    datos = api(f"/competitions/{liga}/scorers?limit=30", ttl=600)
    lista = []
    for s in datos.get("scorers", []):
        j = s.get("player") or {}
        lista.append({
            "id": j.get("id"), "nombre": j.get("name", ""),
            "nacionalidad": j.get("nationality") or "", "nacimiento": j.get("dateOfBirth") or "",
            "posicion": j.get("section") or j.get("position") or "",
            "equipo": equipo(s.get("team")),
            "goles": s.get("goals") or 0, "asistencias": s.get("assists") or 0,
            "penales": s.get("penalties") or 0, "partidos": s.get("playedMatches") or 0,
        })
    return {"liga": liga, "nombre": LIGAS[liga][0], "goleadores": lista}


def datos_equipos(liga):
    if liga in HL_LIGAS:
        vistos, lista = set(), []
        for g in hl_tabla(liga)["grupos"]:
            for f in g["filas"]:
                if f["equipo"]["id"] not in vistos:
                    vistos.add(f["equipo"]["id"])
                    lista.append(f["equipo"])
        return {"liga": liga, "equipos": sorted(lista, key=lambda t: t["nombre"])}
    datos = api(f"/competitions/{liga}/teams", ttl=86400)
    lista = [equipo(t) for t in datos.get("teams", [])]
    lista.sort(key=lambda t: t["nombre"])
    return {"liga": liga, "equipos": lista}


def proximos_fd(id_equipo, cantidad=5):
    """Próximos partidos de verdad: solo desde hoy en adelante, ordenados por fecha."""
    hoy = datetime.now(ARG).date()
    try:
        datos = api(f"/teams/{id_equipo}/matches?dateFrom={hoy.isoformat()}&dateTo={(hoy + timedelta(days=120)).isoformat()}", ttl=900)
    except Exception:
        datos = api(f"/teams/{id_equipo}/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED", ttl=900)
    limite = time.time() - 3 * 3600
    lista = [m for m in datos.get("matches", [])
             if m.get("status") in ("SCHEDULED", "TIMED", "IN_PLAY", "PAUSED")
             and a_hora_arg(m["utcDate"]).timestamp() > limite]
    lista.sort(key=lambda m: m["utcDate"])
    return [partido(m) for m in lista[:cantidad]]


def datos_proximos(id_equipo):
    if str(id_equipo).startswith("h"):
        return {"partidos": proximos_hl(str(id_equipo), 3)}
    return {"partidos": proximos_fd(id_equipo, 3)}


# ---------------------------------------------------------------------------
# Highlightly: Liga Profesional, Copa Argentina, Libertadores y Sudamericana
# Plan gratis = 100 pedidos por día. "Uso inteligente":
#   - fuera del horario de partidos no se consulta nada (se usa lo guardado)
#   - durante los partidos se reparte lo que queda del día: 75% para la Liga Profesional
#   - solo se consulta cuando alguien está mirando la página
# Clave en la variable HIGHLIGHTLY_KEY. Opcional: HL_IDS="LPF=123,CAR=456,LIB=789,SUD=321"
# ---------------------------------------------------------------------------
HL_KEY = os.environ.get("HIGHLIGHTLY_KEY", "").strip()
HL_BASE = "https://soccer.highlightly.net"
HL_LIGAS = {
    "LPF": ("Liga Profesional", "Argentina"),
    "CAR": ("Copa Argentina", "Argentina"),
    "LIB": ("Copa Libertadores", "Sudamérica"),
    "SUD": ("Copa Sudamericana", "Sudamérica"),
}
# Formas de buscar cada torneo (se prueban en orden hasta encontrarlo; cada búsqueda se guarda 30 días)
HL_BUSQUEDA = {
    "LPF": [{"countryCode": "AR"}, {"countryName": "Argentina"}],
    "CAR": [{"countryCode": "AR"}, {"countryName": "Argentina"}],
    "LIB": [{"leagueName": "CONMEBOL Libertadores"}, {"leagueName": "Copa Libertadores"}, {"leagueName": "Libertadores"},
            {"countryName": "South America"}, {"countryName": "World"}],
    "SUD": [{"leagueName": "CONMEBOL Sudamericana"}, {"leagueName": "Copa Sudamericana"}, {"leagueName": "Sudamericana"},
            {"countryName": "South America"}, {"countryName": "World"}],
}
_EXCLUIR = (" reserva", " reserve", " femen", " women", " sub ", " u20", " u17", " u23", " youth", " juvenil", " futsal")


def _elige(cod, nombre_norm):
    if any(x in nombre_norm for x in _EXCLUIR):
        return False
    return {"LPF": " liga profesional " in nombre_norm or nombre_norm.strip() == "primera division",
            "CAR": " copa argentina " in nombre_norm,
            "LIB": " libertadores " in nombre_norm or " libertadores" in nombre_norm,
            "SUD": " sudamericana " in nombre_norm or " sul americana " in nombre_norm or " sudamericana" in nombre_norm}[cod]


VIVO_HL = {"First half", "Second half", "Extra time", "Penalties", "In progress", "Half time", "Break time"}
PAUSA_HL = {"Half time", "Break time"}
_hl = {"restantes": None, "limite": None, "usados": 0, "dia": None, "error": ""}
_hl_candado = threading.Lock()


def _dia_cupo():
    return datetime.now(timezone.utc).date()


def hl(ruta, params=None, ttl=3600):
    """Pide a Highlightly. Si se terminó el cupo o falla, devuelve lo último guardado."""
    clave = "hl" + ruta + json.dumps(params or {}, sort_keys=True)
    ahora = time.time()
    viejo = _memoria.get(clave)
    if viejo and ahora - viejo[0] < ttl:
        return viejo[1]
    if not HL_KEY:
        raise RuntimeError("Falta cargar la clave de Highlightly (HIGHLIGHTLY_KEY) en Render.")
    with _hl_candado:
        if _hl["dia"] != _dia_cupo():
            _hl.update(dia=_dia_cupo(), usados=0, restantes=None)
        if _hl["restantes"] is not None and _hl["restantes"] <= 1:
            if viejo:
                return viejo[1]
            raise RuntimeError("Se terminaron los pedidos gratis de hoy. Mañana se actualiza solo.")
        try:
            r = requests.get(HL_BASE + ruta, params=params, headers={"x-rapidapi-key": HL_KEY}, timeout=15)
        except Exception as e:
            _hl["error"] = str(e)[:120]
            if viejo:
                return viejo[1]
            raise RuntimeError("Highlightly no responde en este momento.")
        _hl["usados"] += 1
        try:
            _hl["restantes"] = int(r.headers.get("x-ratelimit-requests-remaining"))
            _hl["limite"] = int(r.headers.get("x-ratelimit-requests-limit"))
        except (TypeError, ValueError):
            pass
    if r.status_code != 200:
        _hl["error"] = f"{r.status_code} {r.text[:120]}"
        if r.status_code == 429:
            _hl["restantes"] = 0
        if viejo:
            return viejo[1]
        raise RuntimeError("Highlightly no devolvió datos (" + str(r.status_code) + ").")
    datos = r.json()
    _memoria[clave] = (ahora, datos)
    return datos


def _lista(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "matches", "leagues", "teams"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def hl_liga(cod):
    """(id, temporada) de cada torneo. Se busca una vez por mes, o se fija con HL_IDS."""
    fijos = dict(x.split("=", 1) for x in os.environ.get("HL_IDS", "").replace(" ", "").split(",") if "=" in x)
    if fijos.get(cod, "").isdigit():
        # número fijado en Render: no gasta pedidos. La temporada es el año actual.
        return int(fijos[cod]), datetime.now(ARG).year
    vistos = []
    for busqueda in HL_BUSQUEDA[cod]:
        offset = 0
        for _ in range(5):
            params = dict(busqueda, limit=100, offset=offset)
            datos = hl("/leagues", params, ttl=30 * 86400)
            ligas = _lista(datos)
            for lg in ligas:
                vistos.append(lg.get("name") or "")
                elegida = (str(lg.get("id")) == fijos[cod]) if fijos.get(cod) else _elige(cod, normalizar(lg.get("name")))
                if elegida:
                    temporadas = [x.get("season") for x in (lg.get("seasons") or []) if isinstance(x, dict) and x.get("season")]
                    return lg.get("id"), (max(temporadas) if temporadas else datetime.now(ARG).year)
            total = ((datos or {}).get("pagination") or {}).get("totalCount") if isinstance(datos, dict) else None
            offset += 100
            if len(ligas) < 100 or (total is not None and offset >= total):
                break
    if fijos.get(cod):
        return int(fijos[cod]), datetime.now(ARG).year
    _hl.setdefault("candidatos", {})[cod] = sorted(set(vistos))[:40]
    raise RuntimeError(f"No encontramos {HL_LIGAS[cod][0]} en Highlightly.")


def _equipo_hl(t):
    t = t or {}
    return {"id": f"h{t['id']}" if t.get("id") is not None else None, "nombre": t.get("name") or "A definir",
            "escudo": t.get("logo") or "", "sigla": ""}


def partido_hl(m, cod):
    st = m.get("state") or {}
    desc = st.get("description") or ""
    marcador = ((st.get("score") or {}).get("current") or "")
    gl = gv = None
    mm = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(marcador))
    vivo = desc in VIVO_HL
    terminado = desc.startswith("Finished") or desc == "Awarded"
    if mm and (vivo or terminado):
        gl, gv = int(mm.group(1)), int(mm.group(2))
    estado = ("PAUSED" if desc in PAUSA_HL else "IN_PLAY") if vivo else "FINISHED" if terminado else \
        "POSTPONED" if desc == "Postponed" else "CANCELLED" if desc in ("Cancelled", "Abandoned") else "TIMED"
    dt = a_hora_arg(str(m.get("date", "")).strip('"'))
    ronda = str(m.get("round") or "")
    num = re.search(r"(\d+)\s*$", ronda)
    lg = m.get("league") or {}
    return {
        "id": f"h{m.get('id')}", "fecha": dt.strftime("%Y-%m-%d"), "hora": dt.strftime("%H:%M"), "estado": estado,
        "vivo": vivo, "terminado": terminado, "minuto": st.get("clock"),
        "jornada": int(num.group(1)) if num and "season" in ronda.lower() else None,
        "fase": None if (num and "season" in ronda.lower()) else ronda,
        "liga": cod, "ligaNombre": HL_LIGAS[cod][0], "ligaEmblema": lg.get("logo") or "",
        "local": _equipo_hl(m.get("homeTeam")), "visitante": _equipo_hl(m.get("awayTeam")),
        "gl": gl, "gv": gv, "_inicio": dt.timestamp(),
    }


def hl_temporada(cod):
    """Fixture completo de la temporada (unas pocas páginas, una vez por día)."""
    id_liga, temporada = hl_liga(cod)
    lista, offset = [], 0
    for _ in range(8):
        d = hl("/matches", {"leagueId": id_liga, "season": temporada, "limit": 100, "offset": offset}, ttl=86400)
        pagina = _lista(d)
        lista += pagina
        total = ((d or {}).get("pagination") or {}).get("totalCount") if isinstance(d, dict) else None
        offset += 100
        if len(pagina) < 100 or (total is not None and offset >= total):
            break
    return [partido_hl(m, cod) for m in lista]


# --- Uso inteligente de los pedidos durante los partidos ---
VENTANA_ANTES, VENTANA_DESPUES = 5 * 60, 125 * 60
VENTANA_MAXIMA = 170 * 60  # si el partido sigue (alargue, penales) se sigue siguiendo hasta acá


def fin_ventana(p):
    return p["_inicio"] + (VENTANA_MAXIMA if p.get("vivo") else VENTANA_DESPUES)


def en_ventana(p, ahora=None):
    ahora = ahora or time.time()
    return not p["terminado"] and p["_inicio"] - VENTANA_ANTES <= ahora <= fin_ventana(p)


# Prioridades: cuánto más seguido se actualiza cada uno, comparado con Boca (1 = el más rápido)
PRIORIDAD = {"boca": 1.0, "river": 1.5, "LPF": 3.0, "otros": 4.0}
MINIMO = {"boca": 1, "river": 2, "LPF": 3, "otros": 4}       # minutos mínimos entre consultas
EQUIPOS_PRIORIDAD = {"boca": " boca juniors ", "river": " river plate "}


def ids_prioridad():
    """Busca los números de Boca y River en lo guardado de la Liga Profesional
    (así no se confunde con River Plate de Uruguay)."""
    ids = {}

    def mirar(nombre, ident):
        for clave, buscado in EQUIPOS_PRIORIDAD.items():
            if normalizar(nombre) == buscado and ident:
                ids[ident] = clave
    for k, (t, d) in list(_memoria.items()):
        if k.startswith("hlhoyLPF"):
            for p in d:
                mirar(p["local"]["nombre"], p["local"]["id"])
                mirar(p["visitante"]["nombre"], p["visitante"]["id"])
        elif k.startswith("hl/matches{") or k.startswith("hl/standings"):
            for m in _lista(d):
                if isinstance(m, dict) and _elige("LPF", normalizar((m.get("league") or {}).get("name"))):
                    for lado in ("homeTeam", "awayTeam"):
                        e = m.get(lado) or {}
                        mirar(e.get("name"), f"h{e['id']}" if e.get("id") is not None else None)
            if isinstance(d, dict) and _elige("LPF", normalizar((d.get("league") or {}).get("name"))):
                for g in d.get("groups") or []:
                    for f in g.get("standings") or []:
                        e = f.get("team") or {}
                        mirar(e.get("name"), f"h{e['id']}" if e.get("id") is not None else None)
    return ids


def prioridad_de(p, ids=None):
    ids = ids if ids is not None else ids_prioridad()
    claves = [ids.get(p["local"]["id"]), ids.get(p["visitante"]["id"])]
    if "boca" in claves:
        return "boca"
    if "river" in claves:
        return "river"
    return "LPF" if p.get("liga") == "LPF" else "otros"


def _minutos_en_vivo_restantes():
    """Minutos de partido que faltan hasta que se renueva el cupo (00:00 UTC), según prioridad."""
    ahora = time.time()
    corte = datetime.combine(_dia_cupo() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()
    ids = ids_prioridad()
    ventanas = {k: [] for k in PRIORIDAD}
    for clave, (t, d) in list(_memoria.items()):
        if not clave.startswith("hlhoy"):
            continue
        for p in d:
            if p["terminado"]:
                continue
            a, b = max(ahora, p["_inicio"] - VENTANA_ANTES), min(corte, fin_ventana(p))
            if b > a:
                ventanas[prioridad_de(p, ids)].append((a, b))
                if prioridad_de(p, ids) in ("boca", "river") and p.get("liga") == "LPF":
                    ventanas["LPF"].append((a, b))  # el pedido general de la liga también corre

    def union(iv):
        total, fin = 0, 0
        for a, b in sorted(iv):
            a = max(a, fin)
            if b > a:
                total += b - a
                fin = b
        return total / 60
    return {k: union(v) for k, v in ventanas.items()}


def intervalos():
    """Reparte los pedidos que quedan en el día: Boca primero, después River, la liga y las copas."""
    restantes = _hl["restantes"] if _hl["restantes"] is not None else 100 - _hl["usados"]
    disponibles = max(0, restantes - 15)  # 15 quedan de reserva para fichas de partidos
    if disponibles <= 0:
        return {k: 60 for k in PRIORIDAD}
    minutos = _minutos_en_vivo_restantes()
    # pedidos necesarios = suma(minutos / intervalo); intervalo = base * prioridad
    base = sum(minutos[k] / PRIORIDAD[k] for k in PRIORIDAD) / (disponibles * 0.9)  # 10% de margen
    return {k: max(MINIMO[k], int(base * PRIORIDAD[k]) + (0 if (base * PRIORIDAD[k]).is_integer() else 1)) for k in PRIORIDAD}


def intervalo_minutos(cod):
    return intervalos()["LPF" if cod == "LPF" else "otros"]


def refrescar_prioritarios(lista):
    """Partidos de Boca y River en vivo: se consultan solos, más seguido que el resto."""
    ids = ids_prioridad()
    iv = None
    for p in lista:
        if not en_ventana(p):
            continue
        clave_p = prioridad_de(p, ids)
        if clave_p not in ("boca", "river"):
            continue
        iv = iv or intervalos()
        try:
            d = hl(f"/matches/{p['id'][1:]}", ttl=iv[clave_p] * 60)
            m = d[0] if isinstance(d, list) and d else d
            nuevo = partido_hl(m, p["liga"])
            for k in ("gl", "gv", "minuto", "estado", "vivo", "terminado"):
                p[k] = nuevo[k]
        except Exception:
            pass
    return lista


def hl_hoy(cod, fecha):
    """Partidos de un torneo en una fecha. Solo se vuelve a consultar dentro del horario de partidos."""
    clave = "hlhoy" + cod + fecha
    hoy = datetime.now(ARG).strftime("%Y-%m-%d")
    if fecha != hoy:
        # otros días: se arman con el fixture de la temporada (no gasta pedidos extra)
        return [p for p in hl_temporada(cod) if p["fecha"] == fecha]
    ahora = time.time()
    guardado = _memoria.get(clave)
    if guardado:
        t, lista = guardado
        hay_partido = any(en_ventana(p, ahora) for p in lista)
        if not hay_partido or ahora - t < intervalo_minutos(cod) * 60:
            return refrescar_prioritarios(lista)  # fuera de horario: pausa. Dentro: solo Boca/River si toca
    id_liga, temporada = hl_liga(cod)
    d = hl("/matches", {"leagueId": id_liga, "season": temporada, "date": fecha, "timezone": "America/Argentina/Buenos_Aires"}, ttl=0)
    lista = [p for p in (partido_hl(m, cod) for m in _lista(d)) if p["fecha"] == fecha]
    _memoria[clave] = (ahora, lista)
    return lista  # recién actualizado: no hace falta pedir Boca/River aparte


def hl_tabla(cod):
    id_liga, temporada = hl_liga(cod)
    d = hl("/standings", {"leagueId": id_liga, "season": temporada}, ttl=6 * 3600)
    grupos = []
    for g in (d or {}).get("groups", []) if isinstance(d, dict) else []:
        filas = []
        for f in g.get("standings", []):
            tot = f.get("total") or {}
            gf, gc = tot.get("scoredGoals") or 0, tot.get("receivedGoals") or 0
            filas.append({"pos": f.get("position"), "equipo": _equipo_hl(f.get("team")), "pj": tot.get("games") or 0,
                          "g": tot.get("wins") or 0, "e": tot.get("draws") or 0, "p": tot.get("loses") or 0,
                          "dg": gf - gc, "pts": f.get("points") or 0, "forma": ""})
        nombre = re.sub(r"^.*? - ", "", g.get("name") or "") if len(d.get("groups", [])) > 1 else ""
        grupos.append({"nombre": nombre, "filas": filas})
    return {"liga": cod, "nombre": HL_LIGAS[cod][0], "grupos": grupos}


def _cod_de_partido(id_txt, m=None):
    for c in HL_LIGAS:
        for k, (t, l) in list(_memoria.items()):
            if k.startswith("hlhoy" + c) and any(p["id"] == id_txt for p in l):
                return c
    nombre_liga = normalizar(((m or {}).get("league") or {}).get("name"))
    return next((c for c in HL_LIGAS if _elige(c, nombre_liga)), "LPF")


def datos_partido_hl(id_num):
    clave = "hl" + f"/matches/{id_num}" + "{}"
    previo = _memoria.get(clave)
    ttl = 0
    if previo:
        m0 = previo[1][0] if isinstance(previo[1], list) and previo[1] else previo[1]
        desc = ((m0 or {}).get("state") or {}).get("description") or ""
        if desc.startswith("Finished"):
            ttl = 86400
        elif desc in VIVO_HL:
            p0 = partido_hl(m0, _cod_de_partido(f"h{id_num}", m0))
            ttl = intervalos()[prioridad_de(p0)] * 60
        else:
            ttl = 1800
    d = hl(f"/matches/{id_num}", ttl=ttl)
    m = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    p = partido_hl(m, _cod_de_partido(f"h{id_num}", m))
    res = dict(p)
    res["medio"] = [None, None]
    res["estadio"] = (m.get("venue") or {}).get("name", "") if isinstance(m.get("venue"), dict) else ""
    res["arbitro"] = (m.get("referee") or {}).get("name", "") if isinstance(m.get("referee"), dict) else ""
    eventos = []
    for e in (m.get("events") or []):
        eventos.append({"minuto": e.get("time") or "", "tipo": e.get("type") or "", "jugador": e.get("player") or "",
                        "asistencia": e.get("assist") or "", "sale": e.get("substituted") or "",
                        "equipo": f"h{(e.get('team') or {}).get('id')}"})
    res["eventos"] = eventos
    res["goles"] = [{"minuto": e["minuto"], "jugador": e["jugador"], "asistencia": e["asistencia"], "equipo": e["equipo"],
                     "tipo": "OWN" if e["tipo"] == "Own Goal" else "PENALTY" if e["tipo"] == "Penalty" else ""}
                    for e in eventos if e["tipo"] in ("Goal", "Own Goal", "Penalty")]
    res["historial"] = None
    res["fuente"] = "hl"
    res["alineacion"] = None
    if p["vivo"] or p["terminado"] or (p["_inicio"] - time.time() < 3600):
        try:
            al = hl(f"/lineups/{id_num}", ttl=86400 if p["terminado"] else 900)
            al = al[0] if isinstance(al, list) and al else al

            def lado(x):
                x = x or {}
                filas = [[{"nombre": j.get("name") or "", "numero": j.get("number")} for j in (fila if isinstance(fila, list) else [fila]) if isinstance(j, dict)]
                         for fila in (x.get("initialLineup") or [])]
                return {"formacion": x.get("formation") or "", "filas": filas,
                        "suplentes": [{"nombre": j.get("name") or "", "numero": j.get("number")} for j in (x.get("substitutes") or []) if isinstance(j, dict)]}
            if isinstance(al, dict) and (al.get("homeTeam") or al.get("awayTeam")):
                res["alineacion"] = {"local": lado(al.get("homeTeam")), "visitante": lado(al.get("awayTeam"))}
        except Exception:
            pass
    res.pop("_inicio", None)
    return res


def hl_equipo(id_txt):
    """Nombre y escudo de un equipo de Highlightly, sacado de lo que ya está guardado."""
    for c in HL_LIGAS:
        try:
            for p in hl_temporada(c):
                for t in (p["local"], p["visitante"]):
                    if t["id"] == id_txt:
                        return t
        except Exception:
            continue
    return {"id": id_txt, "nombre": "Equipo", "escudo": "", "sigla": ""}


def proximos_hl(id_txt, cantidad=5):
    ahora = time.time()
    lista = []
    for c in HL_LIGAS:
        try:
            lista += [p for p in hl_temporada(c) if not p["terminado"] and p["_inicio"] > ahora - 3 * 3600
                      and p["estado"] in ("TIMED", "IN_PLAY", "PAUSED")
                      and id_txt in (p["local"]["id"], p["visitante"]["id"])]
        except Exception:
            continue
    lista.sort(key=lambda p: p["_inicio"])
    return [{k: v for k, v in p.items() if k != "_inicio"} for p in lista[:cantidad]]


def datos_estadisticas_hl(id_num):
    d = hl(f"/statistics/{id_num}", ttl=600)
    equipos = d if isinstance(d, list) else _lista(d)
    nombres, filas = {}, {}
    for i, eq in enumerate(equipos[:2]):
        for s in (eq.get("statistics") or []):
            n = s.get("displayName") or s.get("name") or ""
            if n:
                filas.setdefault(n, [None, None])[i] = s.get("value")
    return {"estadisticas": [{"nombre": n, "local": v[0], "visitante": v[1]} for n, v in filas.items()]}


def hl_estado():
    return {"clave": bool(HL_KEY), "usadosHoy": _hl["usados"], "restantes": _hl["restantes"], "limite": _hl["limite"],
            "ultimoError": _hl["error"],
            "intervalosMinutos": intervalos(), "minutosDePartidoQueFaltanHoy": {k: round(v) for k, v in _minutos_en_vivo_restantes().items()},
            "equiposPrioridad": ids_prioridad(),
            "ligas": {c: _estado_liga(c) for c in HL_LIGAS}, "nombresEncontrados": _hl.get("candidatos", {})}


def _estado_liga(c):
    if not HL_KEY:
        return None
    try:
        i, t = hl_liga(c)
        return {"id": i, "temporada": t}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Resumen en YouTube: busca el video del partido con la API oficial de YouTube
# (gratis: 100 búsquedas por día). La clave va en la variable YOUTUBE_API_KEY.
# ---------------------------------------------------------------------------
YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
CANALES_CONFIABLES = ["espn", "laliga", "premier league", "dazn", "tnt sports", "fox sports", "cbs sports", "bein",
                      "uefa", "serie a", "bundesliga", "ligue 1", "movistar", "tyc sports", "nbc sports", "sky sports",
                      "telemundo", "univision", "disney", "star+", "conmebol", "afa", "real madrid", "fc barcelona"]
_videos = {}


def datos_resumen(id_partido):
    p = datos_partido_hl(str(id_partido)[1:]) if str(id_partido).startswith("h") else partido(api(f"/matches/{id_partido}", ttl=300))
    busqueda = f'{p["local"]["nombre"]} vs {p["visitante"]["nombre"]} resumen'
    respuesta = {"video": None, "busqueda": "https://www.youtube.com/results?search_query=" + requests.utils.quote(busqueda + " " + p["fecha"][:4])}
    if not YOUTUBE_KEY or not p["terminado"]:
        respuesta["motivo"] = "sin_clave" if not YOUTUBE_KEY else "no_termino"
        return respuesta
    if id_partido in _videos:
        respuesta["video"] = _videos[id_partido]
        return respuesta
    desde = (date.fromisoformat(p["fecha"]) - timedelta(days=1)).isoformat() + "T00:00:00Z"
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=12, params={
            "part": "snippet", "q": busqueda, "type": "video", "maxResults": 8, "order": "relevance",
            "publishedAfter": desde, "relevanceLanguage": "es", "videoEmbeddable": "true", "key": YOUTUBE_KEY})
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception:
        items = []
    nl, nv = normalizar(p["local"]["nombre"]), normalizar(p["visitante"]["nombre"])

    def puntaje(it):
        s = it.get("snippet", {})
        tit, canal = normalizar(s.get("title")), normalizar(s.get("channelTitle"))
        pts = 0
        pts += 2 if any(w in tit for w in nl.split() if len(w) > 3) else 0
        pts += 2 if any(w in tit for w in nv.split() if len(w) > 3) else 0
        pts += 2 if any(k in tit for k in (" resumen ", " highlights ", " goles ", " goals ")) else 0
        pts += 3 if any(" " + c + " " in canal for c in CANALES_CONFIABLES) else 0
        return pts
    if items:
        mejor = max(items, key=puntaje)
        s = mejor.get("snippet", {})
        miniaturas = s.get("thumbnails", {})
        foto = (miniaturas.get("maxres") or miniaturas.get("high") or miniaturas.get("medium") or miniaturas.get("default") or {}).get("url", "")
        vid = (mejor.get("id") or {}).get("videoId")
        if vid and puntaje(mejor) >= 4:
            _videos[id_partido] = {"id": vid, "titulo": s.get("title", ""), "canal": s.get("channelTitle", ""),
                                  "portada": foto or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                                  "enlace": f"https://www.youtube.com/watch?v={vid}"}
            respuesta["video"] = _videos[id_partido]
    return respuesta


# ---------------------------------------------------------------------------
# Ficha de clubes y jugadores: football-data.org + TheSportsDB (fotos, estadio, camiseta, clubes anteriores)
# ---------------------------------------------------------------------------
TSDB = "https://www.thesportsdb.com/api/v1/json/123"
_tsdb_turnos = deque()
_tsdb_candado = threading.Lock()


_tsdb_caido = {"hasta": 0}


def tsdb(ruta, ttl=86400):
    """Pide datos a TheSportsDB (gratis, 30 pedidos por minuto). Nunca hace esperar a la página:
    si se pasó del límite o no responde, devuelve vacío y sigue."""
    clave = "tsdb" + ruta
    ahora = time.time()
    if clave in _memoria and ahora - _memoria[clave][0] < ttl:
        return _memoria[clave][1]
    if ahora < _tsdb_caido["hasta"]:
        return {}
    with _tsdb_candado:
        while _tsdb_turnos and ahora - _tsdb_turnos[0] > 60:
            _tsdb_turnos.popleft()
        if len(_tsdb_turnos) >= 25:
            return {}
        _tsdb_turnos.append(ahora)
    try:
        r = requests.get(TSDB + ruta, timeout=6, headers={"User-Agent": NAVEGADOR})
        datos = r.json() if r.status_code == 200 else {}
        if r.status_code == 429:
            return {}
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        _tsdb_caido["hasta"] = time.time() + 300  # no responde: dejamos de preguntar 5 minutos
        return {}
    except Exception:
        datos = {}
    _memoria[clave] = (ahora, datos)
    return datos


def primera_lista(d):
    for v in (d or {}).values():
        if isinstance(v, list) and v:
            return v
    return []


def buscar_equipo_tsdb(nombres):
    from urllib.parse import quote
    vistos = set()
    for n in nombres:
        n = (n or "").strip()
        for variante in (n, re.sub(r"\b(FC|CF|AFC|SC|CA|AC|SSC|FK|SK)\b", "", n).strip()):
            if not variante or variante in vistos:
                continue
            vistos.add(variante)
            for t in primera_lista(tsdb("/searchteams.php?t=" + quote(variante))):
                if (t.get("strSport") or "Soccer") == "Soccer":
                    return t
    return {}



# ---------------------------------------------------------------------------
# Wikipedia: datos del club, foto del estadio y los colores de las camisetas
# (como las dibuja Wikipedia). Sirve para todos los clubes, sobre todo los argentinos.
# ---------------------------------------------------------------------------
WIKI_AGENTE = "TikiData/1.0 (https://tikidata.onrender.com; app de futbol)"
FICHAS = {"es": "Ficha de equipo de fútbol", "en": "Infobox football club"}
NO_ES_EL_CLUB = ["femenino", "women", "reserva", "baloncesto", "basketball", "futsal", "fútbol sala", "voleibol",
                 "balonmano", "rugby", "hockey", "anexo", "historia de", "temporada", "season", "sub-", "u-2", "u2", "ii)", " b)"]
PARTES_KIT = ["la", "b", "ra", "sh", "so"]
COLORES_KIT = ["leftarm", "body", "rightarm", "shorts", "socks"]


def wiki(idioma, params, ttl=7 * 86400):
    clave = "wiki" + idioma + json.dumps(params, sort_keys=True)
    ahora = time.time()
    if clave in _memoria and ahora - _memoria[clave][0] < ttl:
        return _memoria[clave][1]
    try:
        r = requests.get(f"https://{idioma}.wikipedia.org/w/api.php", timeout=6, headers={"User-Agent": WIKI_AGENTE},
                         params=dict(params, format="json", formatversion=2))
        datos = r.json() if r.status_code == 200 else {}
    except Exception:
        return {}
    _memoria[clave] = (ahora, datos)
    return datos


def _ficha(texto, nombre_ficha):
    """Recorta la ficha (el cuadro de la derecha) del texto de la página."""
    m = re.search(r"\{\{\s*(?:" + re.escape(nombre_ficha) + r"|Infobox football club|Ficha de club deportivo)", texto, re.I)
    if not m:
        return ""
    nivel, i = 0, m.start()
    while i < len(texto) - 1:
        par = texto[i:i + 2]
        if par == "{{":
            nivel += 1
            i += 2
            continue
        if par == "}}":
            nivel -= 1
            i += 2
            if nivel == 0:
                return texto[m.start():i]
            continue
        i += 1
    return texto[m.start():]


def _campo(ficha, *nombres):
    for n in nombres:
        m = re.search(r"\n\s*\|\s*" + re.escape(n) + r"\s*=([^\n]*)", ficha, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _limpio(v):
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", v or "", flags=re.S)
    v = re.sub(r"<br\s*/?>", ", ", v)
    v = re.sub(r"<[^>]+>", "", v)
    v = re.sub(r"\[\[(?:Archivo|File|Imagen|Image):[^\]]*\]\]", "", v, flags=re.I)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\{\{(?:bandera|flagicon|flag|bandera2)[^}]*\}\}", "", v, flags=re.I)
    v = re.sub(r"\{\{(?:fecha de inicio|start date|fecha)\|([^{}]*)\}\}", lambda m: " ".join(m.group(1).split("|")), v, flags=re.I)
    v = re.sub(r"\{\{[^{}|]*\|([^{}|]*)[^{}]*\}\}", r"\1", v)
    v = re.sub(r"\{\{[^}]*\}\}", "", v).replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", v).strip(" ,;")


def _color(v):
    v = _limpio(v).lstrip("#").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3}", v):
        return "#" + v
    return v.lower() if re.fullmatch(r"[A-Za-z]{3,20}", v) else ""


def _camisetas_wiki(ficha):
    kits = []
    for n, tipo in (("1", "Titular"), ("2", "Suplente"), ("3", "Alternativa")):
        colores = {c: _color(_campo(ficha, c + n)) for c in COLORES_KIT}
        if not colores["body"]:
            continue
        dibujo = {p: re.sub(r"[^\w\-.]", "", _limpio(_campo(ficha, "pattern_" + p + n)))[:60] for p in PARTES_KIT}
        kits.append({"tipo": tipo, "colores": colores, "dibujo": dibujo})
    return kits


PARTES_HTML = {"left_arm": "la", "body": "b", "right_arm": "ra", "shorts": "sh", "socks": "so"}
NOMBRES_KIT = ["Titular", "Suplente", "Alternativa"]


def _camisetas_html(idioma, titulo):
    """Copia las camisetas tal cual las dibuja Wikipedia: cada parte (mangas, cuerpo, short y medias)
    es un dibujo pixelado encima de un color de fondo."""
    d = wiki(idioma, {"action": "parse", "page": titulo, "prop": "text", "section": 0, "redirects": 1,
                      "disableeditsection": 1, "disablelimitreport": 1}) or {}
    html = ((d.get("parse") or {}).get("text")) or ""
    if isinstance(html, dict):
        html = html.get("*", "")
    kits, actual = [], None
    for m in re.finditer(r'<img[^>]*?\ssrc="([^"]*?Kit_(left_arm|body|right_arm|shorts|socks)[^"]*)"', html):
        parte = PARTES_HTML[m.group(2)]
        antes = html[max(0, m.start() - 800):m.start()]
        estilos = [e for e in re.findall(r'style="([^"]*)"', antes) if "background" in e]
        color = ""
        if estilos:
            c = re.search(r"background(?:-color)?\s*:\s*([^;\"]+)", estilos[-1])
            color = c.group(1).strip() if c else ""
        if not re.fullmatch(r"#[0-9A-Fa-f]{3,8}|[A-Za-z]{3,20}|rgb\([\d,\s]+\)", color):
            color = ""
        src = m.group(1).replace("&amp;", "&")
        src = "https:" + src if src.startswith("//") else src
        if parte == "la" or actual is None or parte in actual["partes"]:
            if len(kits) >= 3:
                break
            actual = {"tipo": NOMBRES_KIT[len(kits)], "partes": {}}
            kits.append(actual)
        pos = {}
        if estilos:
            for k in ("left", "top", "width", "height"):
                v = re.search(r"(?:^|;)\s*" + k + r"\s*:\s*(-?\d+(?:\.\d+)?)px", estilos[-1])
                if v:
                    pos[k] = float(v.group(1))
        actual["partes"][parte] = {"color": color, "img": src, "pos": pos}
    return [k for k in kits if "b" in k["partes"]]


GENERICAS = {"club", "atletico", "atlético", "deportivo", "sociedad", "sport", "sporting", "football", "futbol",
             "fútbol", "real", "city", "united", "union", "unión", "the", "and", "del", "de", "la", "los", "las"}


def _palabras_clave(n):
    return [w for w in normalizar(re.sub(r"\b(FC|CF|AFC|SC|CA|AC|SSC|FK|SK|CD|CS)\b", "", n or "")).split()
            if len(w) > 2 and w not in GENERICAS]


def _colores_ficha(ficha):
    """Color de la camiseta titular según Wikipedia (cuerpo, pantalón, manga)."""
    salida = []
    for campo in ("body1", "shorts1", "leftarm1"):
        c = _color(_campo(ficha, campo))
        if c and c not in salida:
            salida.append(c)
    return salida


def _buscar_pagina(idioma, nombres, pais=""):
    for extra in ([" " + pais] if pais else []) + [""]:
        for n in nombres:
            n = (n or "").strip()
            if not n:
                continue
            claves = _palabras_clave(n)
            d = wiki(idioma, {"action": "query", "list": "search", "srlimit": 8,
                              "srsearch": f'{n}{extra} hastemplate:"{FICHAS[idioma]}"'})
            for r in ((d.get("query") or {}).get("search") or []):
                t = r.get("title", "")
                tn = normalizar(t)
                if any(x in t.lower() for x in NO_ES_EL_CLUB):
                    continue
                if claves and sum(" " + w + " " in tn for w in claves) < max(1, (len(claves) + 1) // 2):
                    continue  # que el título tenga el nombre del club (evita confundir clubes)
                return t
    return ""


def datos_wiki(nombres, pais=""):
    """Busca el club en Wikipedia (primero en español) y devuelve sus datos."""
    for idioma in ("es", "en"):
        titulo = _buscar_pagina(idioma, nombres, pais)
        if not titulo:
            continue
        d = wiki(idioma, {"action": "query", "titles": titulo, "redirects": 1,
                          "prop": "revisions|extracts", "rvprop": "content", "rvslots": "main",
                          "exintro": 1, "explaintext": 1, "exsentences": 6})
        paginas = (d.get("query") or {}).get("pages") or []
        if not paginas or "revisions" not in paginas[0]:
            continue
        texto = paginas[0]["revisions"][0].get("slots", {}).get("main", {}).get("content", "")
        ficha = _ficha(texto, FICHAS[idioma])
        if not ficha:
            continue
        estadio_crudo = _campo(ficha, "estadio", "ground")
        enlace = re.search(r"\[\[([^\]|#]+)", estadio_crudo)
        capacidad = re.search(r"\d[\d.,\s]{2,9}\d", _limpio(_campo(ficha, "capacidad", "capacity")))
        fundacion = re.search(r"\b(?:18|19|20)\d{2}\b", _limpio(_campo(ficha, "fundación", "fundacion", "founded")))
        res = {
            "idioma": idioma, "pagina": f"https://{idioma}.wikipedia.org/wiki/" + titulo.replace(" ", "_"),
            "estadio": _limpio(estadio_crudo).split(",")[0][:80],
            "capacidad": int(re.sub(r"\D", "", capacidad.group(0))) if capacidad else None,
            "fundacion": int(fundacion.group(0)) if fundacion else None,
            "ubicacion": _limpio(_campo(ficha, "ubicación", "ubicacion", "ciudad", "location"))[:80],
            "apodo": _limpio(_campo(ficha, "apodo", "apodos", "nickname"))[:80],
            "dt": _limpio(_campo(ficha, "entrenador", "manager", "head_coach", "coach"))[:60],
            "descripcion": (paginas[0].get("extract") or "")[:700] if idioma == "es" else "",
            "camisetas": [], "coloresKit": _colores_ficha(ficha), "fotoEstadio": "",
        }
        if enlace:  # foto del estadio: la imagen principal de la página del estadio
            e = wiki(idioma, {"action": "query", "titles": enlace.group(1).strip(), "redirects": 1,
                              "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 1200})
            pe = (e.get("query") or {}).get("pages") or []
            if pe and pe[0].get("thumbnail"):
                res["fotoEstadio"] = pe[0]["thumbnail"]["source"]
        return res
    return {}


def datos_club(id_equipo):
    """Datos básicos del club: salen rápido (football-data.org)."""
    if str(id_equipo).startswith("h"):
        t = hl_equipo(str(id_equipo))
        return {"id": t["id"], "nombre": t["nombre"], "corto": t["nombre"], "escudo": t["escudo"], "fundacion": None,
                "estadio": "", "direccion": "", "web": "", "colores": "", "dt": "", "plantel": [],
                "capacidad": None, "ubicacion": "", "fotoEstadio": "", "camisetas": [], "descripcion": "",
                "proximos": proximos_hl(str(id_equipo), 5)}
    t = api(f"/teams/{id_equipo}", ttl=86400)
    eq = equipo(t)
    club = {
        "id": eq["id"], "nombre": t.get("name") or eq["nombre"], "corto": eq["nombre"], "escudo": eq["escudo"],
        "fundacion": t.get("founded"), "estadio": t.get("venue") or "", "direccion": t.get("address") or "",
        "web": t.get("website") or "", "colores": t.get("clubColors") or "",
        "dt": (t.get("coach") or {}).get("name") or "",
        "plantel": [{"id": j.get("id"), "nombre": j.get("name", ""), "posicion": j.get("position") or "",
                     "nacimiento": j.get("dateOfBirth") or "", "nacionalidad": j.get("nationality") or "", "foto": ""}
                    for j in (t.get("squad") or [])],
        "capacidad": None, "ubicacion": "", "fotoEstadio": "", "camisetas": [], "descripcion": "",
    }
    try:
        club["proximos"] = proximos_fd(id_equipo, 5)
    except Exception:
        club["proximos"] = []
    return club


COLORES_EN = {"claret": "#7A263A", "skyblue": "#6CABDD", "lightblue": "#8CC8F0", "royalblue": "#2350A0",
              "navyblue": "#13235B", "navy": "#13235B", "burgundy": "#800020", "bordeaux": "#800020", "maroon": "#7B1E2B",
              "gold": "#E8B923", "amber": "#FFBF00", "sky": "#6CABDD", "celeste": "#75AADB", "azul": "#1F4FA0",
              "rojo": "#D32027", "blanco": "#FFFFFF", "negro": "#111111", "amarillo": "#F6D200", "verde": "#1B8A3C",
              "grana": "#7A1F2B", "bordo": "#800020", "naranja": "#F27C21", "violeta": "#5B2C83"}


def _color_nombre(n):
    n = re.sub(r"[^a-z]", "", (n or "").lower())
    return COLORES_EN.get(n) or (n if 3 <= len(n) <= 20 else "")


def kits_por_colores(colores):
    """Si no hay foto ni datos de Wikipedia: camisetas lisas con los colores del club."""
    c = [x for x in colores if x][:3]
    if not c:
        return []
    c1, c2 = c[0], (c[1] if len(c) > 1 else "#FFFFFF")
    def kit(cuerpo, pantalon, medias):
        return {"leftarm": cuerpo, "body": cuerpo, "rightarm": cuerpo, "shorts": pantalon, "socks": medias}
    sin_dibujo = {p: "" for p in PARTES_KIT}
    return [{"tipo": "Titular", "colores": kit(c1, c2, c1), "dibujo": sin_dibujo, "aprox": True},
            {"tipo": "Suplente", "colores": kit(c2, c1, c2), "dibujo": sin_dibujo, "aprox": True}]


def _temporada_actual(texto):
    anios = [int(a) for a in re.findall(r"(?:19|20)\d{2}", texto or "")]
    return bool(anios) and max(anios) >= datetime.now(ARG).year - 1



# ---------------------------------------------------------------------------
# Camisetas pixeladas: el diseño TRADICIONAL de cada club, cargado a mano.
# Diseños: liso, franja (horizontal, como Boca), banda (diagonal, como River), rayas (verticales),
# rayas3 (tres colores), mitad, aros (rayas horizontales), centro (raya vertical al medio), v (como Vélez)
# Cada club: (diseño, color1, color2, opciones). Opciones: m = mangas, k = cuello, c3 = tercer color,
# sup = camiseta suplente con el mismo formato. "arg": solo si el club es argentino (evita confusiones).
# ---------------------------------------------------------------------------
BL, NE, RO, CE, AZ, VE, AM, GR, NA = "#FFFFFF", "#161616", "#D5121E", "#6CB4E8", "#1F3F99", "#13803B", "#F7D117", "#7A1F2B", "#14235A"
ORO, AZB, MA, CR, CEL = "#F5C400", "#1D4AA0", "#6B3E1F", "#EFE3C2", "#8FC9F0"
CAMISETAS = {
    # --- Argentina: Liga Profesional ---
    "boca juniors": ("franja", AZB, ORO, {"sup": ("liso", ORO, AZB, {})}),
    "river plate": ("banda", BL, RO, {"k": NE, "sup": ("banda", NE, RO, {})}),
    "racing": ("rayas", CE, BL, {"m": BL, "arg": True, "sup": ("liso", NA, CE, {})}),
    "independiente": ("liso", RO, BL, {"k": RO, "arg": True, "sup": ("liso", BL, RO, {})}),
    "san lorenzo": ("rayas", AZ, RO, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "huracan": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "velez": ("v", BL, AZ, {"sup": ("v", AZ, BL, {})}),
    "estudiantes": ("rayas", RO, BL, {"m": RO, "arg": True, "sup": ("liso", BL, RO, {})}),
    "gimnasia": ("franja", BL, NA, {"arg": True, "sup": ("franja", NA, BL, {})}),
    "lanus": ("liso", GR, BL, {"sup": ("liso", BL, GR, {})}),
    "banfield": ("rayas", VE, BL, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "argentinos juniors": ("liso", RO, BL, {"sup": ("banda", BL, RO, {})}),
    "rosario central": ("rayas", AZ, AM, {"m": AZ, "sup": ("liso", AM, AZ, {})}),
    "newell": ("mitad", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "talleres": ("rayas", AZ, BL, {"m": AZ, "arg": True, "sup": ("liso", BL, AZ, {})}),
    "belgrano": ("liso", CE, BL, {"sup": ("liso", NE, CE, {})}),
    "instituto": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "godoy cruz": ("rayas", AZ, BL, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "independiente rivadavia": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "gimnasia mendoza": ("rayas", BL, NE, {"m": BL, "sup": ("liso", NE, BL, {})}),
    "gimnasia y esgrima mendoza": ("rayas", BL, NE, {"m": BL, "sup": ("liso", NE, BL, {})}),
    "atletico tucuman": ("rayas", CE, BL, {"m": CE, "sup": ("liso", AZ, BL, {})}),
    "central cordoba": ("rayas", NE, BL, {"m": NE, "sup": ("liso", BL, NE, {})}),
    "defensa y justicia": ("liso", AM, VE, {"sup": ("liso", VE, AM, {})}),
    "platense": ("franja", BL, MA, {"sup": ("liso", MA, BL, {})}),
    "tigre": ("franja", AZ, RO, {"arg": True, "sup": ("liso", BL, AZ, {})}),
    "sarmiento": ("liso", VE, BL, {"arg": True, "sup": ("liso", BL, VE, {})}),
    "barracas central": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "riestra": ("liso", NE, BL, {"sup": ("liso", BL, NE, {})}),
    "aldosivi": ("rayas", VE, AM, {"m": VE, "sup": ("liso", AM, VE, {})}),
    "union": ("rayas", RO, BL, {"m": RO, "arg": True, "sup": ("liso", BL, RO, {})}),
    "san martin de san juan": ("rayas", VE, NE, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "san martin s j": ("rayas", VE, NE, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "newells": ("mitad", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "san martin sj": ("rayas", VE, NE, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "san martin de tucuman": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "estudiantes de rio cuarto": ("rayas", CE, BL, {"m": CE, "sup": ("liso", BL, CE, {})}),
    # --- Argentina: ascenso ---
    "colon": ("mitad", RO, NE, {"arg": True, "sup": ("liso", BL, RO, {})}),
    "chacarita": ("rayas3", RO, BL, {"c3": NE, "m": NE, "sup": ("liso", BL, NE, {})}),
    "quilmes": ("liso", BL, AZ, {"arg": True, "sup": ("liso", AZ, BL, {})}),
    "ferro carril oeste": ("liso", VE, BL, {"sup": ("liso", BL, VE, {})}),
    "ferro": ("liso", VE, BL, {"arg": True, "sup": ("liso", BL, VE, {})}),
    "atlanta": ("rayas", AZ, AM, {"arg": True, "m": AZ, "sup": ("liso", AM, AZ, {})}),
    "all boys": ("liso", BL, NE, {"sup": ("liso", NE, BL, {})}),
    "nueva chicago": ("rayas", VE, NE, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "almirante brown": ("rayas", AM, NE, {"m": AM, "sup": ("liso", NE, AM, {})}),
    "temperley": ("liso", CE, BL, {"sup": ("liso", BL, CE, {})}),
    "patronato": ("rayas", RO, NE, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "arsenal de sarandi": ("banda", CE, RO, {"sup": ("liso", RO, CE, {})}),
    "arsenal sarandi": ("banda", CE, RO, {"sup": ("liso", RO, CE, {})}),
    "chaco for ever": ("rayas", NE, BL, {"m": NE, "sup": ("liso", BL, NE, {})}),
    # --- Sudamérica ---
    "penarol": ("rayas", AM, NE, {"m": NE, "sup": ("liso", BL, NE, {})}),
    "nacional": ("liso", BL, AZ, {"sup": ("liso", AZ, BL, {})}),
    "defensor sporting": ("liso", "#5B2C83", BL, {"sup": ("liso", BL, "#5B2C83", {})}),
    "colo colo": ("liso", BL, NE, {"sup": ("liso", NE, BL, {})}),
    "universidad de chile": ("liso", AZ, RO, {"sup": ("liso", BL, AZ, {})}),
    "universidad catolica": ("franja", BL, AZ, {"sup": ("liso", AZ, BL, {})}),
    "olimpia": ("franja", BL, NE, {"sup": ("liso", NE, BL, {})}),
    "cerro porteno": ("rayas", AZ, RO, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "libertad": ("rayas", BL, NE, {"m": NE, "sup": ("liso", NE, BL, {})}),
    "ldu": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "liga de quito": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "barcelona sc": ("liso", AM, NE, {"sup": ("liso", NE, AM, {})}),
    "barcelona guayaquil": ("liso", AM, NE, {"sup": ("liso", NE, AM, {})}),
    "independiente del valle": ("rayas", NA, NE, {"m": NE, "sup": ("liso", BL, NA, {})}),
    "emelec": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "atletico nacional": ("rayas", VE, BL, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "millonarios": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "america de cali": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "junior": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "deportes tolima": ("liso", GR, AM, {"sup": ("liso", AM, GR, {})}),
    "universitario": ("liso", CR, GR, {"sup": ("liso", GR, CR, {})}),
    "alianza lima": ("rayas", AZ, BL, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "sporting cristal": ("liso", CE, BL, {"sup": ("liso", BL, CE, {})}),
    "bolivar": ("liso", CE, BL, {"sup": ("liso", BL, CE, {})}),
    "the strongest": ("rayas", AM, NE, {"m": AM, "sup": ("liso", NE, AM, {})}),
    "flamengo": ("aros", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "palmeiras": ("liso", VE, BL, {"sup": ("liso", BL, VE, {})}),
    "corinthians": ("liso", BL, NE, {"sup": ("liso", NE, BL, {})}),
    "sao paulo": ("franja", BL, RO, {"sup": ("rayas3", RO, BL, {"c3": NE})}),
    "santos": ("liso", BL, NE, {"sup": ("rayas", NE, BL, {"m": NE})}),
    "fluminense": ("rayas3", GR, VE, {"c3": BL, "m": BL, "sup": ("liso", BL, GR, {})}),
    "botafogo": ("rayas", NE, BL, {"m": NE, "sup": ("liso", BL, NE, {})}),
    "vasco": ("banda", NE, BL, {"sup": ("banda", BL, NE, {})}),
    "gremio": ("rayas3", CE, NE, {"c3": BL, "m": NE, "sup": ("liso", BL, CE, {})}),
    "internacional": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "atletico mineiro": ("rayas", NE, BL, {"m": NE, "sup": ("liso", BL, NE, {})}),
    "cruzeiro": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "bahia": ("liso", BL, AZ, {"sup": ("rayas3", AZ, RO, {"c3": BL})}),
    "fortaleza": ("rayas3", AZ, RO, {"c3": BL, "m": AZ, "sup": ("liso", BL, AZ, {})}),
    "athletico": ("aros", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "bragantino": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "vitoria": ("aros", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "sport recife": ("aros", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "ceara": ("rayas", NE, BL, {"m": NE, "sup": ("liso", BL, NE, {})}),
    "juventude": ("rayas", VE, BL, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "mirassol": ("liso", AM, VE, {"sup": ("liso", VE, AM, {})}),
    # --- Europa ---
    "real madrid": ("liso", BL, "#3D195B", {"sup": ("liso", NE, BL, {})}),
    "barcelona": ("rayas", "#0B4EA2", "#A50044", {"m": "#0B4EA2", "k": "#A50044", "sup": ("liso", AM, "#A50044", {})}),
    "atletico madrid": ("rayas", RO, BL, {"m": RO, "k": AZ, "sup": ("liso", AZ, RO, {})}),
    "atletico de madrid": ("rayas", RO, BL, {"m": RO, "k": AZ, "sup": ("liso", AZ, RO, {})}),
    "athletic": ("rayas", RO, BL, {"m": RO, "k": NE, "sup": ("liso", NE, RO, {})}),
    "real betis": ("rayas", VE, BL, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "sevilla": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "valencia": ("liso", BL, NE, {"sup": ("liso", NE, BL, {})}),
    "villarreal": ("liso", AM, AZ, {"sup": ("liso", AZ, AM, {})}),
    "real sociedad": ("rayas", AZ, BL, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "juventus": ("rayas", BL, NE, {"m": NE, "sup": ("liso", NE, BL, {})}),
    "inter": ("rayas", "#0A3D91", NE, {"m": "#0A3D91", "sup": ("liso", BL, "#0A3D91", {})}),
    "milan": ("rayas", RO, NE, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "napoli": ("liso", CEL, BL, {"sup": ("liso", BL, CEL, {})}),
    "roma": ("liso", "#8E1F2F", "#F0B323", {"sup": ("liso", BL, "#8E1F2F", {})}),
    "lazio": ("liso", CEL, BL, {"sup": ("liso", NE, CEL, {})}),
    "fiorentina": ("liso", "#5B2C83", BL, {"sup": ("liso", BL, "#5B2C83", {})}),
    "atalanta": ("rayas", NE, "#1E5BB8", {"m": NE, "sup": ("liso", BL, "#1E5BB8", {})}),
    "bayern": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "dortmund": ("liso", AM, NE, {"sup": ("liso", NE, AM, {})}),
    "leverkusen": ("liso", RO, NE, {"sup": ("liso", NE, RO, {})}),
    "paris": ("centro", NA, RO, {"m": NA, "sup": ("liso", BL, NA, {})}),
    "psg": ("centro", NA, RO, {"m": NA, "sup": ("liso", BL, NA, {})}),
    "marseille": ("liso", BL, CE, {"sup": ("liso", CE, BL, {})}),
    "manchester united": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "man united": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "manchester city": ("liso", CEL, BL, {"sup": ("liso", NA, CEL, {})}),
    "man city": ("liso", CEL, BL, {"sup": ("liso", NA, CEL, {})}),
    "liverpool": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "arsenal": ("liso", RO, BL, {"m": BL, "sup": ("liso", AM, AZ, {})}),
    "chelsea": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "tottenham": ("liso", BL, NA, {"sup": ("liso", NA, BL, {})}),
    "newcastle": ("rayas", BL, NE, {"m": NE, "sup": ("liso", NE, BL, {})}),
    "aston villa": ("liso", "#7A263A", CEL, {"m": CEL, "sup": ("liso", CEL, "#7A263A", {})}),
    "west ham": ("liso", "#7A263A", CEL, {"m": CEL, "sup": ("liso", BL, "#7A263A", {})}),
    "everton": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "benfica": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "porto": ("rayas", AZ, BL, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "sporting": ("aros", VE, BL, {"sup": ("liso", BL, VE, {})}),
    "ajax": ("centro", BL, RO, {"ancho": True, "sup": ("liso", AZ, BL, {})}),
    "psv": ("rayas", RO, BL, {"m": RO, "sup": ("liso", NE, RO, {})}),
    "feyenoord": ("mitad", RO, BL, {"sup": ("liso", NE, RO, {})}),
    "celtic": ("aros", VE, BL, {"sup": ("liso", BL, VE, {})}),
}
NAR, CLA, AMB, VIO, GRIS, VINO = "#F27C21", "#7A263A", "#F5A300", "#5B2C83", "#9AA0A8", "#6B1A2C"


def K(p, c1, c2, sup=None, **op):
    """Atajo para cargar un club: diseño, colores y la suplente (por defecto, lisa invertida)."""
    op["sup"] = sup or ("liso", BL if c1 != BL else c2, c1 if c1 != BL else BL, {})
    if op["sup"][1] == op["sup"][2]:
        op["sup"] = ("liso", NE, c1, {})
    return (p, c1, c2, op)


def S(p, c1, c2, **op):
    return (p, c1, c2, op)


CAMISETAS.update({
    # --- Inglaterra ---
    "bournemouth": K("rayas", RO, NE, m=RO), "brentford": K("rayas", RO, BL, m=RO, k=NE),
    "brighton": K("rayas", AZ, BL, m=AZ), "burnley": K("liso", CLA, CEL, m=CEL), "crystal palace": K("rayas", RO, AZ, m=AZ),
    "fulham": K("liso", BL, NE), "leeds": K("liso", BL, AZ), "nottingham forest": K("liso", RO, BL),
    "sunderland": K("rayas", RO, BL, m=RO, k=NE), "wolverhampton": K("liso", "#FDB913", NE), "wolves": K("liso", "#FDB913", NE),
    "leicester": K("liso", AZ, BL), "ipswich": K("liso", AZ, BL), "southampton": K("rayas", RO, BL, m=RO, k=NE),
    "sheffield united": K("rayas", RO, BL, m=RO, k=NE), "sheffield wednesday": K("rayas", AZ, BL, m=AZ),
    "west bromwich": K("rayas", NA, BL, m=NA), "west brom": K("rayas", NA, BL, m=NA), "middlesbrough": K("liso", RO, BL),
    "norwich": K("liso", AM, VE), "watford": K("liso", AM, NE), "coventry": K("liso", CEL, BL), "stoke": K("rayas", RO, BL, m=RO),
    "queens park rangers": K("aros", AZ, BL), "qpr": K("aros", AZ, BL), "millwall": K("liso", NA, BL),
    "hull": K("rayas", AMB, NE, m=AMB), "derby": K("liso", BL, NE), "bristol city": K("liso", RO, BL), "swansea": K("liso", BL, NE),
    "cardiff": K("liso", AZ, BL), "preston": K("liso", BL, NA), "blackburn": K("mitad", AZ, BL), "birmingham": K("liso", AZ, BL),
    "wrexham": K("liso", RO, BL), "charlton": K("liso", RO, BL), "portsmouth": K("liso", AZ, BL), "oxford": K("liso", AM, NA),
    "luton": K("liso", NAR, NA), "plymouth": K("liso", VE, BL), "barnsley": K("liso", RO, BL), "blackpool": K("liso", NAR, BL),
    # --- España ---
    "alaves": K("rayas", AZ, BL, m=AZ), "celta": K("liso", CEL, BL), "elche": K("franja", BL, VE), "espanyol": K("rayas", AZ, BL, m=AZ),
    "getafe": K("liso", AZ, BL), "girona": K("rayas", RO, BL, m=RO), "levante": K("rayas", AZ, GR, m=AZ),
    "mallorca": K("liso", RO, NE), "osasuna": K("liso", RO, NA), "rayo vallecano": K("banda", BL, RO),
    "oviedo": K("liso", AZ, BL), "las palmas": K("liso", AM, AZ), "leganes": K("rayas", AZ, BL, m=AZ), "valladolid": K("rayas", VIO, BL, m=VIO),
    "deportivo la coruna": K("rayas", AZ, BL, m=AZ), "real zaragoza": K("liso", BL, AZ), "sporting gijon": K("rayas", RO, BL, m=RO),
    "racing santander": K("liso", BL, VE),
    # --- Italia ---
    "bologna": K("rayas", RO, NA, m=NA), "cagliari": K("mitad", RO, NA), "como": K("liso", AZ, BL), "cremonese": K("rayas", RO, GRIS, m=RO),
    "genoa": K("mitad", RO, NA), "verona": K("liso", NA, AM, k=AM), "lecce": K("rayas", AM, RO, m=AM), "parma": K("liso", BL, NE),
    "pisa": K("rayas", AZ, NE, m=NE), "sassuolo": K("rayas", VE, NE, m=VE), "torino": K("liso", GR, BL), "udinese": K("rayas", BL, NE, m=NE),
    "sampdoria": K("franja", AZ, BL), "empoli": K("liso", AZ, BL), "monza": K("mitad", RO, BL), "venezia": K("liso", NE, NAR),
    "salernitana": K("liso", GR, BL), "frosinone": K("liso", AM, AZ),
    # --- Alemania ---
    "augsburg": K("liso", BL, RO), "monchengladbach": K("liso", BL, VE), "gladbach": K("liso", BL, VE),
    "eintracht frankfurt": K("liso", NE, RO, k=RO), "frankfurt": K("liso", NE, RO, k=RO), "freiburg": K("liso", RO, NE),
    "hamburger": K("liso", BL, RO), "hamburg": K("liso", BL, RO), "heidenheim": K("liso", RO, AZ), "hoffenheim": K("liso", AZ, BL),
    "koln": K("liso", BL, RO), "mainz": K("liso", RO, BL), "leipzig": K("liso", BL, RO), "st pauli": K("liso", MA, BL),
    "stuttgart": K("franja", BL, RO), "union berlin": K("liso", RO, BL), "werder": K("liso", VE, BL), "bremen": K("liso", VE, BL),
    "wolfsburg": K("liso", "#65B32E", BL), "bochum": K("liso", AZ, BL), "holstein kiel": K("liso", AZ, BL), "schalke": K("liso", AZ, BL),
    "hertha": K("rayas", AZ, BL, m=AZ),
    # --- Francia ---
    "angers": K("rayas", BL, NE, m=NE), "auxerre": K("liso", BL, AZ), "brest": K("liso", RO, BL), "le havre": K("mitad", CEL, NA),
    "lens": K("liso", AM, RO, m=RO), "lille": K("liso", RO, NA), "lorient": K("liso", NAR, NE), "lyon": K("liso", BL, AZ),
    "metz": K("liso", GR, BL), "monaco": K("diag", RO, BL), "nantes": K("liso", AM, VE), "nice": K("rayas", RO, NE, m=NE),
    "paris fc": K("liso", NA, BL), "rennes": K("liso", RO, NE, m=NE), "strasbourg": K("liso", AZ, BL), "toulouse": K("liso", VIO, BL),
    "saint etienne": K("liso", VE, BL), "montpellier": K("liso", NA, NAR), "reims": K("liso", RO, BL),
    # --- Portugal ---
    "braga": K("liso", RO, BL, m=BL), "guimaraes": K("liso", BL, NE), "boavista": K("cuadros", NE, BL), "famalicao": K("liso", AZ, BL),
    "gil vicente": K("liso", RO, AZ), "rio ave": K("rayas", VE, BL, m=VE), "moreirense": K("liso", VE, BL), "arouca": K("liso", AM, AZ),
    "estoril": K("rayas", AM, AZ, m=AM), "casa pia": K("liso", NE, BL), "santa clara": K("liso", RO, BL),
    "tondela": K("rayas", AM, VE, m=VE), "alverca": K("liso", RO, BL), "estrela": K("rayas", RO, VE, m=RO), "avs": K("liso", RO, BL),
    "nacional madeira": K("rayas", BL, NE, m=NE),
    # --- Países Bajos ---
    "az": K("liso", RO, BL), "alkmaar": K("liso", RO, BL), "twente": K("liso", RO, BL), "utrecht": K("liso", RO, BL),
    "heerenveen": K("rayas", AZ, BL, m=BL), "groningen": K("mitad", VE, BL), "nec": K("rayas3", RO, VE, c3=NE, m=NE),
    "go ahead eagles": K("mitad", RO, AM), "sparta rotterdam": K("rayas", RO, BL, m=RO), "fortuna sittard": K("liso", AM, VE),
    "heracles": K("rayas", BL, NE, m=NE), "zwolle": K("liso", AZ, BL), "nac breda": K("liso", AM, NE), "excelsior": K("liso", RO, NE),
    "volendam": K("liso", NAR, NE), "telstar": K("liso", BL, NE), "willem ii": K("rayas3", RO, BL, c3=AZ, m=AZ),
    # --- Brasil (más) ---
    "remo": K("liso", NA, BL), "coritiba": K("aros", BL, VE), "chapecoense": K("liso", VE, BL), "atletico goianiense": K("rayas", RO, NE, m=RO),
    "goias": K("liso", VE, BL), "america mineiro": K("liso", VE, NE), "cuiaba": K("liso", AM, VE), "paysandu": K("rayas", CE, BL, m=CE),
    "criciuma": K("rayas", AM, NE, m=AM), "avai": K("rayas", AZ, BL, m=AZ), "ponte preta": K("banda", BL, NE), "guarani": K("rayas", AM, NE, m=NE),
    "nautico": K("rayas", RO, BL, m=RO), "santa cruz": K("aros", RO, NE), "novorizontino": K("liso", AM, NE), "operario": K("liso", NE, BL),
    # --- Argentina: más ascenso ---
    "defensores de belgrano": K("rayas", RO, NE, m=RO), "estudiantes de buenos aires": K("rayas", BL, NE, m=NE),
    "estudiantes caseros": K("rayas", BL, NE, m=NE), "mitre": K("rayas", AM, NE, m=AM), "racing de cordoba": K("rayas", CE, BL, m=CE),
    "racing cordoba": K("rayas", CE, BL, m=CE), "san telmo": K("rayas", CE, BL, m=CE), "atletico rafaela": K("rayas", CE, BL, m=CE),
    "gimnasia de jujuy": K("rayas", CE, BL, m=CE), "gimnasia jujuy": K("rayas", CE, BL, m=CE), "gimnasia y tiro": K("rayas", BL, CE, m=BL),
    "tristan suarez": K("rayas", AZ, AM, m=AZ), "alvarado": K("rayas", AZ, BL, m=AZ), "deportivo madryn": K("rayas", NE, AM, m=NE),
    "guillermo brown": K("rayas", RO, BL, m=RO), "villa dalmine": K("rayas", VIO, BL, m=VIO), "deportivo maipu": K("rayas", RO, BL, m=RO),
    "agropecuario": K("liso", VE, AM), "ciudad de bolivar": K("liso", AZ, BL), "san miguel": K("rayas", VE, BL, m=VE),
    "deportivo moron": K("liso", BL, RO), "los andes": K("franja", RO, BL), "almagro": K("rayas3", AZ, BL, c3=NE, m=NE),
    "comunicaciones": K("rayas", NE, AM, m=NE), "dock sud": K("rayas", RO, BL, m=RO), "fenix": K("liso", NE, AM), "midland": K("liso", RO, VE),
    "flandria": K("rayas", VE, BL, m=VE), "excursionistas": K("rayas", VE, BL, m=VE), "acassuso": K("liso", CE, BL),
    "deportivo armenio": K("rayas3", RO, AZ, c3=NAR, m=RO), "argentino de quilmes": K("rayas", CE, BL, m=CE),
    "deportivo riestra": K("liso", NE, BL), "juventud unida": K("liso", BL, AZ), "sportivo italiano": K("liso", AZ, BL),
    "colegiales": K("rayas3", AZ, RO, c3=AM, m=AZ), "ferrocarril midland": K("liso", RO, VE), "san martin de burzaco": K("liso", AM, NE),
    "talleres de remedios": K("rayas", RO, BL, m=RO), "olimpo": K("franja", AM, NE), "villa mitre": K("liso", BL, NE),
    "sol de mayo": K("liso", AZ, BL), "cipolletti": K("rayas", NE, BL, m=NE), "sansinena": K("rayas", RO, BL, m=RO),
    "douglas haig": K("rayas", RO, NE, m=RO), "sportivo belgrano": K("liso", VE, BL), "juventud antoniana": K("rayas", AZ, BL, m=AZ),
    "central norte": K("rayas", NE, BL, m=NE), "chaco": K("rayas", NE, BL, m=NE), "sarmiento de resistencia": K("liso", VE, BL),
    "boca unidos": K("franja", AZ, AM), "crucero del norte": K("liso", AM, NE), "huracan las heras": K("liso", BL, RO),
    "deportivo camioneros": K("liso", AZ, BL), "germinal": K("liso", BL, RO), "independiente chivilcoy": K("liso", RO, BL),
    "union de sunchales": K("liso", BL, AZ), "kimberley": K("rayas", BL, VE, m=BL), "argentino de merlo": K("rayas", BL, CE, m=BL),
    "lujan": K("rayas", BL, RO, m=BL), "berazategui": K("rayas", NA, AM, m=NA), "canuelas": K("liso", RO, NE),
    # --- Sudamérica (más) ---
    "danubio": K("banda", BL, NE), "montevideo wanderers": K("rayas", BL, NE, m=NE), "boston river": K("liso", VE, BL),
    "cerro largo": K("liso", AM, AZ), "plaza colonia": K("liso", VE, BL), "racing montevideo": K("rayas", VE, BL, m=VE),
    "cobreloa": K("liso", NAR, BL), "huachipato": K("rayas", AZ, NE, m=NE), "palestino": K("rayas3", VE, BL, c3=RO, m=VE),
    "everton de vina": K("liso", ORO, AZ), "audax italiano": K("liso", VE, BL), "coquimbo": K("liso", AM, NE), "o higgins": K("liso", CE, BL),
    "union espanola": K("liso", RO, BL), "deportes iquique": K("liso", CE, BL),
    "guarani asuncion": K("rayas", AM, NE, m=NE), "nacional asuncion": K("rayas3", AZ, BL, c3=RO, m=AZ), "sportivo luqueno": K("rayas", AZ, AM, m=AZ),
    "sportivo ameliano": K("liso", CE, BL), "sportivo trinidense": K("liso", BL, RO), "tacuary": K("rayas", BL, NE, m=NE),
    "aucas": K("liso", AM, RO), "deportivo cuenca": K("liso", RO, BL), "universidad catolica quito": K("liso", CE, BL), "el nacional": K("liso", RO, AZ),
    "macara": K("liso", CE, BL), "orense": K("liso", VE, BL), "mushuc runa": K("liso", RO, BL),
    "union santa fe": K("rayas", RO, BL, m=RO, arg=True), "independiente santa fe": K("liso", RO, BL, m=BL), "santa fe": K("liso", RO, BL, m=BL), "deportivo cali": K("liso", VE, BL),
    "once caldas": K("liso", BL, NE), "medellin": K("liso", RO, AZ), "bucaramanga": K("liso", AM, VE), "deportivo pereira": K("rayas", AM, RO, m=AM),
    "melgar": K("mitad", RO, NE), "cienciano": K("liso", RO, BL), "cusco": K("liso", AM, NE), "sport huancayo": K("liso", RO, BL),
    "always ready": K("liso", BL, RO), "wilstermann": K("liso", RO, AZ), "blooming": K("liso", CE, BL), "oriente petrolero": K("liso", VE, BL),
    "san antonio bulo bulo": K("liso", VE, BL), "nacional potosi": K("liso", BL, AZ),
    "caracas": K("liso", RO, BL), "deportivo tachira": K("rayas", AM, NE, m=AM), "universidad central": K("liso", BL, AZ),
    "carabobo": K("liso", GR, BL), "monagas": K("liso", AZ, BL), "metropolitanos": K("liso", VIO, BL), "academia puerto cabello": K("liso", AZ, AM),
})

# --- Selecciones (solo si el nombre es exactamente el país) ---
SELECCIONES = {
    "argentina": K("rayas", CE, BL, sup=("liso", NA, CE, {}), m=BL, k=NE), "brazil": K("liso", AM, VE, sup=("liso", AZ, BL, {}), k=VE),
    "brasil": K("liso", AM, VE, sup=("liso", AZ, BL, {}), k=VE), "uruguay": K("liso", "#5CBFEB", NE), "france": K("liso", NA, BL, k=RO),
    "francia": K("liso", NA, BL, k=RO), "england": K("liso", BL, NA, sup=("liso", RO, BL, {})), "inglaterra": K("liso", BL, NA, sup=("liso", RO, BL, {})),
    "spain": K("liso", RO, AM), "espana": K("liso", RO, AM), "germany": K("liso", BL, NE), "alemania": K("liso", BL, NE),
    "portugal": K("liso", RO, VE), "netherlands": K("liso", NAR, NE), "paises bajos": K("liso", NAR, NE), "belgium": K("liso", RO, NE),
    "belgica": K("liso", RO, NE), "italy": K("liso", "#1A55A6", BL), "italia": K("liso", "#1A55A6", BL), "croatia": K("cuadros", RO, BL),
    "croacia": K("cuadros", RO, BL), "colombia": K("liso", AM, AZ, sup=("liso", NA, AM, {})), "mexico": K("liso", VE, BL, k=RO),
    "united states": K("liso", BL, NA), "usa": K("liso", BL, NA), "estados unidos": K("liso", BL, NA), "canada": K("liso", RO, BL),
    "japan": K("liso", NA, BL), "japon": K("liso", NA, BL), "south korea": K("liso", RO, NE), "korea republic": K("liso", RO, NE),
    "corea del sur": K("liso", RO, NE), "morocco": K("liso", RO, VE), "marruecos": K("liso", RO, VE), "senegal": K("liso", BL, VE),
    "ecuador": K("liso", AM, AZ), "paraguay": K("rayas", RO, BL, m=AZ, k=AZ), "chile": K("liso", RO, AZ), "peru": K("banda", BL, RO),
    "bolivia": K("liso", VE, BL), "venezuela": K("liso", VINO, AM), "switzerland": K("liso", RO, BL), "suiza": K("liso", RO, BL),
    "austria": K("liso", RO, BL), "norway": K("liso", RO, NA), "noruega": K("liso", RO, NA), "scotland": K("liso", NA, BL),
    "escocia": K("liso", NA, BL), "australia": K("liso", AM, VE), "saudi arabia": K("liso", VE, BL), "arabia saudita": K("liso", VE, BL),
    "qatar": K("liso", "#8A1538", BL), "iran": K("liso", BL, RO), "egypt": K("liso", RO, NE), "egipto": K("liso", RO, NE),
    "tunisia": K("liso", RO, BL), "tunez": K("liso", RO, BL), "algeria": K("liso", BL, VE), "argelia": K("liso", BL, VE),
    "ghana": K("liso", BL, NE), "ivory coast": K("liso", NAR, VE), "cote d ivoire": K("liso", NAR, VE), "costa de marfil": K("liso", NAR, VE),
    "south africa": K("liso", AM, VE), "sudafrica": K("liso", AM, VE), "cape verde": K("liso", AZ, RO), "cabo verde": K("liso", AZ, RO),
    "new zealand": K("liso", BL, NE), "nueva zelanda": K("liso", BL, NE), "panama": K("liso", RO, AZ), "haiti": K("liso", AZ, RO),
    "uzbekistan": K("liso", BL, AZ), "jordan": K("liso", BL, RO), "jordania": K("liso", BL, RO), "turkey": K("liso", RO, BL),
    "turkiye": K("liso", RO, BL), "turquia": K("liso", RO, BL), "denmark": K("liso", RO, BL), "dinamarca": K("liso", RO, BL),
    "poland": K("liso", BL, RO), "polonia": K("liso", BL, RO), "sweden": K("liso", AM, AZ), "suecia": K("liso", AM, AZ),
    "czechia": K("liso", RO, AZ), "czech republic": K("liso", RO, AZ), "ukraine": K("liso", AM, AZ), "serbia": K("liso", RO, AZ),
    "wales": K("liso", RO, VE), "gales": K("liso", RO, VE), "ireland": K("liso", VE, BL), "republic of ireland": K("liso", VE, BL),
    "curacao": K("liso", AZ, AM), "nigeria": K("liso", VE, BL), "cameroon": K("liso", VE, RO), "camerun": K("liso", VE, RO),
    "iraq": K("liso", VE, BL), "costa rica": K("liso", RO, AZ), "honduras": K("liso", BL, AZ), "jamaica": K("liso", AM, VE),
    "dr congo": K("liso", AZ, RO), "bosnia and herzegovina": K("liso", AZ, AM), "slovakia": K("liso", BL, AZ), "hungary": K("liso", RO, BL),
    "greece": K("liso", BL, AZ), "romania": K("liso", AM, AZ), "kosovo": K("liso", AZ, AM), "north macedonia": K("liso", RO, AM),
}

_CLAVES_KIT = sorted(CAMISETAS, key=len, reverse=True)
COLORES_FD = {"red": RO, "white": BL, "black": NE, "blue": AZ, "navyblue": NA, "navy": NA, "royalblue": AZ,
              "skyblue": CEL, "lightblue": CEL, "green": VE, "yellow": AM, "gold": ORO, "orange": "#F27C21",
              "claret": "#7A263A", "maroon": GR, "burgundy": GR, "purple": "#5B2C83", "violet": "#5B2C83",
              "grey": "#8A8F98", "gray": "#8A8F98", "silver": "#C0C4CC", "amber": "#FFBF00", "brown": MA}


def _kit(t, tipo):
    diseno, c1, c2, op = t
    return {"tipo": tipo, "pix": {"p": diseno, "c1": c1, "c2": c2, "c3": op.get("c3", ""), "m": op.get("m", ""),
                                  "k": op.get("k", ""), "ancho": bool(op.get("ancho"))}}


def camisetas_pixel(nombres, argentino=False, colores_fd="", colores_wiki=None):
    """Titular y suplente tradicionales del club, para dibujar pixeladas."""
    for n in nombres:  # selecciones: el nombre tiene que ser exactamente el país
        t = SELECCIONES.get(normalizar(n).strip())
        if t:
            return [_kit(t, "Titular"), _kit(t[3]["sup"], "Suplente")]
    for n in nombres:
        tn = normalizar(n)
        for clave in _CLAVES_KIT:
            if all(" " + w + " " in tn for w in clave.split() if w not in ("de", "del", "y", "la")):
                t = CAMISETAS[clave]
                if t[3].get("arg") and not argentino:
                    continue
                return [_kit(t, "Titular"), _kit(t[3]["sup"], "Suplente")] if "sup" in t[3] else [_kit(t, "Titular")]
    # Clubes que no están en la lista: los colores del club (football-data), lisa
    colores = [COLORES_FD.get(re.sub(r"[^a-z]", "", c.lower()))
               for c in re.split(r"\s*/\s*|\s*,\s*|\s+and\s+", colores_fd or "")]
    colores = [c for c in colores if c]
    if not colores and colores_wiki:  # último recurso: los colores de la ficha de Wikipedia
        colores = [c for c in colores_wiki if c]
    if not colores:
        return []
    c1, c2 = colores[0], (colores[1] if len(colores) > 1 else (BL if colores[0] != BL else NE))
    return [_kit(("liso", c1, c2, {}), "Titular"), _kit(("liso", c2, c1, {}), "Suplente")]


def es_argentino(id_txt):
    if not id_txt.startswith("h"):
        return False
    for c in ("LPF", "CAR"):
        try:
            if any(id_txt in (p["local"]["id"], p["visitante"]["id"]) for p in hl_temporada(c)):
                return True
        except Exception:
            continue
    return False


def datos_club_extra(id_equipo):
    """Estadio, foto, camisetas e historia: TheSportsDB + Wikipedia. Se carga después."""
    from concurrent.futures import ThreadPoolExecutor
    if str(id_equipo).startswith("h"):
        n = hl_equipo(str(id_equipo))["nombre"]
        t = {"shortName": n, "name": n, "squad": None}
    else:
        t = api(f"/teams/{id_equipo}", ttl=86400)
    nombres = [t.get("shortName"), t.get("name")]
    pais = "Argentina" if es_argentino(str(id_equipo)) else ""
    res = {"capacidad": None, "ubicacion": "", "fundacion": None, "estadio": "", "descripcion": "",
           "fotoEstadio": "", "camisetas": [], "fotos": {}, "apodo": "", "dt": "", "wikipedia": ""}
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_wiki = ex.submit(datos_wiki, nombres, pais)
        extra = buscar_equipo_tsdb(nombres)
        venue, kits, jugadores = {}, [], []
        if extra:
            id_t, id_v = extra.get("idTeam"), extra.get("idVenue")
            with ThreadPoolExecutor(max_workers=3) as ex2:
                f_venue = ex2.submit(tsdb, f"/lookupvenue.php?id={id_v}") if id_v else None
                f_jug = ex2.submit(tsdb, f"/lookup_all_players.php?id={id_t}") if id_t and t.get("squad") is None else None
                venue = (primera_lista(f_venue.result()) or [{}])[0] if f_venue else {}
                jugadores = primera_lista(f_jug.result()) if f_jug else []
        try:
            w = f_wiki.result(timeout=20) or {}
        except Exception:
            w = {}
    extra = extra or {}
    # Datos: Wikipedia en español primero (nombres en castellano), si no TheSportsDB
    es = w.get("idioma") == "es"
    res["estadio"] = (w.get("estadio") if es else "") or extra.get("strStadium") or w.get("estadio") or ""
    res["ubicacion"] = (w.get("ubicacion") if es else "") or extra.get("strLocation") or venue.get("strLocation") or w.get("ubicacion") or ""
    res["capacidad"] = extra.get("intStadiumCapacity") or venue.get("intCapacity") or w.get("capacidad")
    res["fundacion"] = extra.get("intFormedYear") or w.get("fundacion")
    res["apodo"], res["dt"], res["wikipedia"] = w.get("apodo", ""), w.get("dt", ""), w.get("pagina", "")
    res["descripcion"] = (w.get("descripcion") or extra.get("strDescriptionES") or extra.get("strDescriptionEN") or "")[:700]
    res["fotoEstadio"] = (venue.get("strThumb") or extra.get("strStadiumThumb") or w.get("fotoEstadio")
                          or venue.get("strFanart1") or extra.get("strFanart1") or "")
    # Camisetas: el diseño tradicional del club, dibujado pixelado
    res["camisetas"] = camisetas_pixel(nombres, bool(pais), t.get("clubColors") or "", w.get("coloresKit"))
    if t.get("squad") is None:  # equipos de Highlightly: plantel desde TheSportsDB
        res["plantel"] = [{"id": jp.get("idPlayer"), "nombre": jp.get("strPlayer") or "", "posicion": jp.get("strPosition") or "",
                           "nacimiento": jp.get("dateBorn") or "", "nacionalidad": jp.get("strNationality") or "",
                           "foto": ""} for jp in jugadores if jp.get("strPlayer")]
    return res


def foto_jugador(nombre):
    from urllib.parse import quote
    for x in primera_lista(tsdb("/searchplayers.php?p=" + quote(nombre), ttl=7 * 86400)):
        if (x.get("strSport") or "Soccer") == "Soccer":
            return {"foto": x.get("strCutout") or x.get("strThumb") or x.get("strRender") or ""}
    return {"foto": ""}


def datos_jugador_extra(nombre):
    from urllib.parse import quote
    j = {}
    for variante in (nombre, " ".join(nombre.split()[-2:]) if len(nombre.split()) > 2 else ""):
        if variante:
            lista = [x for x in primera_lista(tsdb("/searchplayers.php?p=" + quote(variante))) if (x.get("strSport") or "Soccer") == "Soccer"]
            if lista:
                j = lista[0]
                break
    if not j:
        return {"encontrado": False}
    anteriores = []
    if j.get("idPlayer"):
        for f in primera_lista(tsdb(f"/lookupformerteams.php?id={j['idPlayer']}")):
            anteriores.append({"club": f.get("strFormerTeam") or "", "escudo": f.get("strBadge") or f.get("strTeamBadge") or "",
                               "desde": (f.get("strJoined") or "")[:4], "hasta": (f.get("strDeparted") or "")[:4],
                               "tipo": f.get("strMoveType") or ""})
    return {"encontrado": True, "foto": j.get("strCutout") or j.get("strThumb") or j.get("strRender") or "",
            "nombre": j.get("strPlayer") or nombre, "equipo": j.get("strTeam") or "", "nacionalidad": j.get("strNationality") or "",
            "nacimiento": j.get("dateBorn") or "", "posicion": j.get("strPosition") or "", "altura": j.get("strHeight") or "",
            "numero": j.get("strNumber") or "", "anteriores": anteriores}


# ---------------------------------------------------------------------------
# Noticias: traer, clasificar por tema, medir importancia y armar la mezcla
# ---------------------------------------------------------------------------
TEMAS = {
    "arg": ("Fútbol argentino", ["liga profesional", "lpf", "boca", "river", "racing", "independiente", "san lorenzo",
             "estudiantes", "velez", "talleres", "lanus", "huracan", "newells", "newell s", "rosario central", "belgrano",
             "argentinos juniors", "gimnasia", "banfield", "tigre", "platense", "instituto", "godoy cruz",
             "defensa y justicia", "barracas central", "sarmiento", "atletico tucuman", "central cordoba",
             "independiente rivadavia", "riestra", "aldosivi", "san martin de san juan", "afa", "copa argentina",
             "torneo clausura", "torneo apertura", "superclasico", "libertadores", "sudamericana", "chiqui tapia"]),
    "asc": ("Ascenso", ["primera nacional", "ascenso", "primera b", "federal a", "chacarita", "atlanta", "all boys",
             "quilmes", "ferro carril oeste", "nueva chicago", "almirante brown", "deportivo moron", "temperley",
             "colon de santa fe", "chaco for ever", "estudiantes de caseros", "los andes", "brown de adrogue",
             "tristan suarez", "agropecuario", "guillermo brown", "gimnasia de jujuy", "atletico rafaela", "san telmo"]),
    "sel": ("Selección Argentina", ["seleccion argentina", "scaloni", "albiceleste", "scaloneta", "eliminatorias"]),
    "top": ("Clubes grandes y clásicos", ["real madrid", "barcelona", "barca", "atletico de madrid", "manchester city",
             "manchester united", "liverpool", "arsenal", "chelsea", "tottenham", "bayern", "psg", "paris saint germain",
             "juventus", "inter de milan", "milan", "napoli", "dortmund", "clasico", "derbi", "champions league", "champions"]),
    "pl": ("Premier League", ["premier league", "premier"]),
    "liga": ("La Liga", ["laliga", "la liga", "liga espanola"]),
    "fich": ("Fichajes", ["fichaje", "fichajes", "ficha por", "mercado de pases", "transferencia", "refuerzo", "traspaso",
             "clausula", "renovacion", "renueva", "firmo", "nuevo club"]),
    "est": ("Estrellas", ["messi", "cristiano", "ronaldo", "mbappe", "haaland", "lamine yamal", "yamal", "vinicius",
             "bellingham", "julian alvarez", "enzo fernandez", "dibu martinez", "lautaro", "mac allister", "neymar",
             "salah", "kane", "pedri", "rodri", "de bruyne", "modric", "lewandowski", "balon de oro"]),
}
IMPORTANTE = ["fallecio", "murio", "muerte", "tragedia", "conmocion", "luto", "grave", "terapia intensiva",
              "internado", "duelo", "sancion", "suspendido", "renuncio", "despedido"]
POCO_INTERES = ["como ver", "donde ver", "a que hora", "por tv", "horario y tv", "en vivo y en directo", "pronostico",
                "apuesta", "apuestas", "cuotas", "horoscopo", "quiniela", "sorteo", "minuto a minuto"]
# Mezcla recomendada si la persona no eligió nada (suma 100)
MEZCLA = [("arg asc sel", 55), ("top pl liga imp", 20), ("est", 15), ("fich otros", 10)]
CANTIDAD = 20

_noticias = {"t": 0, "lista": []}


def normalizar(texto):
    import unicodedata
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def tiene(texto_norm, palabras):
    return any(" " + p + " " in texto_norm for p in palabras)


def imagen_de(item):
    for hijo in item:
        etiqueta = hijo.tag.lower()
        if (etiqueta.endswith("}content") or etiqueta.endswith("}thumbnail")) and hijo.get("url"):
            return hijo.get("url")
        if etiqueta == "enclosure" and "image" in (hijo.get("type") or "image") and hijo.get("url"):
            return hijo.get("url")
        for nieto in hijo:
            if nieto.tag.lower().endswith("}content") and nieto.get("url"):
                return nieto.get("url")
    desc = (item.findtext("description") or "") + (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
    m = re.search(r'<img[^>]+src="([^"]+)"', desc)
    return m.group(1) if m else ""


def leer_feed(fuente, url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": NAVEGADOR})
        raiz = ET.fromstring(r.content)
    except Exception as e:
        _estado_feeds[url[:120]] = "no responde (" + type(e).__name__ + ")"
        return []
    salida = []
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        if not titulo or not enlace:
            continue
        origen = fuente
        if not fuente:
            fuente_tag = item.find("source")
            sitio = (fuente_tag.get("url") if fuente_tag is not None else "") or ""
            origen = next((nom for nom, dom in SITIOS.items() if dom in sitio), "")
            if not origen:
                continue  # solo Olé, TyC Sports, ESPN y Marca
            if " - " in titulo:
                titulo = titulo.rsplit(" - ", 1)[0]
        try:
            cuando = parsedate_to_datetime(item.findtext("pubDate")).astimezone(ARG)
        except Exception:
            cuando = None
        resumen = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        salida.append({"fuente": origen, "titulo": titulo, "enlace": enlace, "imagen": imagen_de(item),
                       "orden": cuando.timestamp() if cuando else 0,
                       "cuando": cuando.strftime("%d/%m %H:%M") if cuando else "",
                       "_texto": normalizar(titulo + " " + resumen[:300]), "_tit": normalizar(titulo)})
    _estado_feeds[url[:120]] = f"{len(salida)} noticias, {sum(1 for n in salida if n['imagen'])} con foto"
    return salida


# --- Fotos: si el portal no manda la foto, se busca dentro de la nota ---
_estado_feeds = {}
_fotos = {}  # enlace original -> (foto, enlace real)


def enlace_google_real(enlace):
    """Google Noticias da un enlace propio; esto averigua la dirección real de la nota."""
    m = re.search(r"/articles/([^?/]+)", enlace)
    if not m:
        return ""
    codigo = m.group(1)
    r = requests.get("https://news.google.com/rss/articles/" + codigo, timeout=8, headers={"User-Agent": NAVEGADOR})
    sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
    if not sg or not ts:
        return ""
    pedido = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
              '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]' % (codigo, ts.group(1), sg.group(1)))
    r2 = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", timeout=8,
                       headers={"User-Agent": NAVEGADOR,
                                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                       data={"f.req": json.dumps([[["Fbv4je", pedido, None, "generic"]]])})
    try:
        datos = json.loads(r2.text.split("\n\n", 1)[1])[:-2]
        return json.loads(datos[0][2])[1]
    except Exception:
        m2 = re.search(r'https?:\\*/\\*/(?!news\.google)[^"\\]+', r2.text)
        return m2.group(0).replace("\\/", "/").replace("\\", "") if m2 else ""


def foto_de_la_nota(url):
    """Lee el comienzo de la nota y saca la foto principal (la que usan las redes)."""
    r = requests.get(url, timeout=7, headers={"User-Agent": NAVEGADOR}, stream=True)
    html = ""
    for parte in r.iter_content(16384, decode_unicode=False):
        html += parte.decode("utf-8", "ignore")
        if "</head>" in html or len(html) > 300000:
            break
    r.close()
    for patron in (r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)',
                   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)'):
        m = re.search(patron, html, re.I)
        if m:
            foto = m.group(1).replace("&amp;", "&")
            return ("https:" + foto) if foto.startswith("//") else foto
    return ""


def completar_foto(n):
    clave = n["enlace"]
    if clave not in _fotos:
        foto, real = "", clave
        try:
            if "news.google." in clave:
                real = enlace_google_real(clave) or ""
            if real:
                foto = foto_de_la_nota(real)
        except Exception:
            pass
        _fotos[clave] = (foto, real or clave)
    n["imagen"], n["enlace"] = _fotos[clave]


_candado_noticias = threading.Lock()


def traer_todas():
    if time.time() - _noticias["t"] < 900 and _noticias["lista"]:
        return _noticias["lista"]
    with _candado_noticias:
        if time.time() - _noticias["t"] < 900 and _noticias["lista"]:
            return _noticias["lista"]
        return _traer_todas()


def _traer_todas():
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote
    pedidos = list(NOTICIAS) + [("", "https://news.google.com/rss/search?q=" + quote(q + " when:2d") +
                                 "&hl=es-419&gl=AR&ceid=AR:es-419") for q in BUSQUEDAS]
    with ThreadPoolExecutor(max_workers=8) as ex:
        resultados = list(ex.map(lambda p: leer_feed(*p), pedidos))
    lista, vistos = [], set()
    for r in resultados:
        for n in r:
            if n["enlace"] in vistos or tiene(n["_tit"], POCO_INTERES):
                continue
            vistos.add(n["enlace"])
            lista.append(n)
    ahora = time.time()
    lista = [n for n in lista if not n["orden"] or ahora - n["orden"] < 4 * 86400]
    # Temas de cada noticia
    for n in lista:
        n["temas"] = [k for k, (_, pal) in TEMAS.items() if tiene(n["_texto"], pal)]
        n["imp"] = tiene(n["_tit"], IMPORTANTE)
    # Agrupar la misma historia contada por distintos portales
    def palabras(n):
        return {w for w in n["_tit"].split() if len(w) > 3}
    lista.sort(key=lambda n: (bool(n["imagen"]), n["orden"]), reverse=True)
    grupos = []
    for n in lista:
        pn = palabras(n)
        for g in grupos:
            pg = g["_pal"]
            if pn and pg and len(pn & pg) / len(pn | pg) >= 0.34:
                g["_fuentes"].add(n["fuente"])
                g["temas"] = sorted(set(g["temas"]) | set(n["temas"]))
                g["imp"] = g["imp"] or n["imp"]
                break
        else:
            n["_pal"], n["_fuentes"] = pn, {n["fuente"]}
            grupos.append(n)
    # Fotos: a las que no traen, se les busca la foto dentro de la nota (las más nuevas primero)
    if len(_fotos) > 3000:
        _fotos.clear()
    sin_foto = sorted([g for g in grupos if not g["imagen"]], key=lambda g: g["orden"], reverse=True)[:70]
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(completar_foto, sin_foto))
    grupos = [g for g in grupos if g["imagen"]]  # todas las noticias con foto
    for g in grupos:
        horas = (ahora - g["orden"]) / 3600 if g["orden"] else 48
        argentina = any(t in ("arg", "asc", "sel") for t in g["temas"])
        g["puntaje"] = ((max(0.0, 48 - horas) / 48) + 1.2 * (len(g["_fuentes"]) - 1) + (1.5 if g["imp"] else 0)
                        + (0.6 if argentina else 0))
    _noticias.update(t=time.time(), lista=grupos)
    return grupos


def datos_noticias(prefs=""):
    grupos = traer_todas()
    elegidos_temas = [p for p in prefs.split(",") if p in TEMAS]
    if elegidos_temas:
        parte = 85 // len(elegidos_temas)
        mezcla = [(t, parte) for t in elegidos_temas] + [("imp", 15)]
    else:
        mezcla = MEZCLA
    ordenadas = sorted(grupos, key=lambda g: g["puntaje"], reverse=True)

    def pertenece(g, cubo):
        claves = cubo.split()
        if "imp" in claves and g["imp"]:
            return True
        if "otros" in claves and not g["temas"]:
            return True
        return any(t in claves for t in g["temas"])

    elegidas, usadas = [], set()
    for cubo, porcentaje in mezcla:
        cupo = round(CANTIDAD * porcentaje / 100)
        for g in ordenadas:
            if cupo <= 0:
                break
            if id(g) not in usadas and pertenece(g, cubo):
                elegidas.append(g)
                usadas.add(id(g))
                cupo -= 1
    for g in ordenadas:
        if len(elegidas) >= CANTIDAD:
            break
        if id(g) not in usadas and (not elegidos_temas or any(t in elegidos_temas for t in g["temas"]) or g["imp"]):
            elegidas.append(g)
            usadas.add(id(g))
    elegidas.sort(key=lambda g: g["puntaje"], reverse=True)
    # Las 4 destacadas: las más importantes, mejor si tienen foto
    destacadas = sorted(elegidas[:8], key=lambda g: g["puntaje"] + (0.8 if g["imagen"] else 0), reverse=True)[:4]
    resto = [g for g in elegidas if g not in destacadas]

    def limpio(g):
        tema = next((TEMAS[t][0] for t in g["temas"]), "")
        return {"fuente": g["fuente"], "titulo": g["titulo"], "enlace": g["enlace"], "imagen": g["imagen"],
                "cuando": g["cuando"], "tema": tema, "importante": g["imp"]}
    return {"destacadas": [limpio(g) for g in destacadas], "noticias": [limpio(g) for g in resto],
            "temas": [[k, v[0]] for k, v in TEMAS.items()]}


# ---------------------------------------------------------------------------
# La página (todo el diseño y el funcionamiento en el navegador)
# ---------------------------------------------------------------------------
PAGINA = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tiki Data — El fútbol en datos</title>
<link rel="icon" href="/logo.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&family=Inter:wght@400;500;600;700;800&display=swap">
<style>
:root { --negro:#05070D; --fondo:#070B14; --tarj:#0D1626; --tarj2:#11203A; --sup:#132039; --linea:#1C2B47;
  --borde:#263A5C; --azul:#4F8FF7; --azulreal:#2F6BDB; --plata:#C9CFD8; --texto:#EEF2F7; --suave:#8E9AAE;
  --vivo:#FF6B73; --verde:#3DD68C; }
* { box-sizing:border-box; }
body { margin:0; background:var(--fondo); color:var(--texto); font-family:Inter, system-ui, sans-serif; }
a { color:inherit; text-decoration:none; }
button { font-family:inherit; color:inherit; cursor:pointer; }
h1, h2, .titulo { font-family:Montserrat, Inter, sans-serif; }
.app { max-width:560px; margin:0 auto; min-height:100vh; padding-bottom:84px; }
header.top { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:8px; padding:10px 12px;
  background:var(--negro); border-bottom:2px solid var(--azulreal); }
.icono { width:44px; height:44px; flex:none; border-radius:12px; border:0; background:var(--sup);
  display:flex; align-items:center; justify-content:center; }
.marca { display:flex; align-items:center; gap:10px; flex:1; min-width:0; }
.marca img { width:44px; height:44px; border-radius:10px; object-fit:cover; }
.marca .nombre { font-family:Montserrat, sans-serif; font-weight:900; font-size:20px; letter-spacing:.5px; line-height:1; color:#E6E9EE; }
.marca .nombre span { color:var(--azul); }
.marca .lema { font-family:Montserrat, sans-serif; font-weight:700; font-size:8px; letter-spacing:1.8px; color:var(--suave); margin-top:4px; }
.vivos { display:flex; gap:8px; overflow-x:auto; padding:10px 12px; background:var(--negro); border-bottom:1px solid var(--linea); scrollbar-width:none; }
.vivos::-webkit-scrollbar { display:none; }
.vivos .tag { flex:none; align-self:center; font-size:11px; font-weight:800; color:var(--vivo); letter-spacing:.6px; }
.chipvivo { flex:none; display:flex; align-items:center; gap:7px; padding:7px 10px; border-radius:10px; background:var(--tarj); border:1px solid var(--linea); font-size:12px; }
.chipvivo b { color:var(--vivo); }
.dias { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)) 44px; gap:6px; padding:10px 12px; background:var(--negro); }
.dia { min-height:48px; border-radius:10px; border:0; background:var(--sup); color:#C7D2E0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; }
.dia small { font-size:11px; font-weight:600; opacity:.8; } .dia b { font-size:15px; }
.dia.on { background:var(--texto); color:var(--fondo); }
main { padding:14px 10px; display:flex; flex-direction:column; gap:14px; }
.tarjeta { background:var(--tarj); border:1px solid var(--linea); border-radius:14px; overflow:hidden; }
.tarjeta.pad { padding:12px 14px; }
.cab { display:flex; align-items:center; gap:10px; padding:10px 12px; background:var(--tarj2); }
.cab .t { flex:1; display:flex; flex-direction:column; } .cab .t b { font-size:14px; } .cab .t small { font-size:11px; color:var(--suave); }
.emb { width:26px; height:26px; border-radius:7px; background:#fff; padding:2px; object-fit:contain; }
.fila { display:grid; grid-template-columns:44px minmax(0,1fr) 62px minmax(0,1fr); align-items:center; gap:6px;
  padding:11px 10px 11px 6px; border-top:1px solid var(--linea); }
.fila .st { font-size:12px; font-weight:700; text-align:center; color:#C7D2E0; }
.fila .st.vivo { color:var(--vivo); }
.fila .eq { display:flex; align-items:center; gap:6px; min-width:0; font-size:13px; font-weight:700; }
.fila .eq.der { justify-content:flex-end; text-align:right; }
.fila .eq.pierde { font-weight:500; color:#9AA8BA; }
.nom { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.esc { width:22px; height:22px; flex:none; object-fit:contain; }
.esc.ph { border-radius:11px; background:#1B2E4F; color:#C7D2E0; font-size:7px; font-weight:800; display:inline-flex; align-items:center; justify-content:center; }
.marc { white-space:nowrap; text-align:center; font-weight:800; font-size:15px; font-variant-numeric:tabular-nums; padding:5px 0; border-radius:8px; color:var(--suave); font-size:12px; }
.marc.jug { background:var(--sup); color:var(--texto); font-size:15px; }
.marc.vivo { background:rgba(255,107,115,.14); color:#FF8A90; font-size:15px; }
.seccion { display:flex; justify-content:space-between; align-items:baseline; padding:4px 4px 0; }
.seccion h2 { margin:0; font-weight:900; font-size:22px; }
.link { font-size:13px; font-weight:600; color:#8DB8FF; padding:8px 0; background:none; border:0; }
.aviso { padding:22px 16px; text-align:center; color:var(--suave); font-size:14px; line-height:1.5; }
.cargando { padding:30px; text-align:center; color:var(--suave); }
.hero { position:relative; display:block; height:300px; border-radius:16px; overflow:hidden; background:var(--sup); }
.hero img { width:100%; height:100%; object-fit:cover; }
.hero .capa { position:absolute; left:0; right:0; bottom:0; padding:70px 16px 16px; background:linear-gradient(to top, rgba(5,7,13,.97) 35%, rgba(5,7,13,0)); }
.fuente { display:inline-block; font-size:11px; font-weight:800; letter-spacing:.6px; padding:3px 8px; border-radius:5px; background:var(--azul); color:var(--fondo); }
.hero .tit { margin-top:10px; font-family:Montserrat, sans-serif; font-weight:800; font-size:20px; line-height:1.2; }
.hero small, .nota small { color:#B4C0D0; font-size:12px; }
.grilla { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; }
.nota { display:flex; flex-direction:column; border-radius:14px; overflow:hidden; background:var(--tarj); border:1px solid var(--linea); }
.nota .img { position:relative; height:112px; background:var(--sup); }
.nota .img img { width:100%; height:100%; object-fit:cover; }
.nota .img .fuente { position:absolute; top:8px; left:8px; font-size:10px; padding:2px 6px; background:rgba(5,7,13,.85); color:var(--texto); }
.nota .txt { padding:10px; display:flex; flex-direction:column; gap:6px; font-weight:700; font-size:13px; line-height:1.3; }
nav.abajo { position:fixed; left:0; right:0; bottom:0; z-index:5; background:var(--negro); border-top:1px solid var(--linea); }
nav.abajo div { max-width:560px; margin:0 auto; display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); padding:6px 4px 14px; }
nav.abajo a, nav.abajo button { display:flex; flex-direction:column; align-items:center; gap:3px; padding:8px 0; font-size:11px; font-weight:600; color:var(--suave); background:none; border:0; }
nav.abajo .on { color:var(--azul); }
.cortina { position:fixed; inset:0; z-index:20; display:none; }
.cortina.abierta { display:flex; }
.cortina aside { width:300px; max-width:85vw; height:100%; overflow-y:auto; background:var(--negro); border-right:2px solid var(--azulreal); padding-bottom:20px; }
.cortina .fondo { flex:1; border:0; background:rgba(5,7,13,.7); }
.cortina .cabm { display:flex; align-items:center; gap:10px; padding:12px; border-bottom:1px solid var(--linea); }
.cortina .cabm b { flex:1; font-family:Montserrat, sans-serif; font-weight:900; font-size:17px; }
.grupo { padding:12px 10px 0; }
.grupo > small { display:block; padding:0 10px 6px; font-size:11px; font-weight:800; letter-spacing:.8px; color:var(--suave); }
.item { display:flex; align-items:center; gap:12px; min-height:44px; padding:0 10px; border-radius:10px; font-size:14px; font-weight:600; }
.item:hover { background:var(--sup); }
.item.off { color:#7A8799; } .item.off em { margin-left:auto; font-style:normal; font-size:10px; font-weight:700; color:var(--suave); }
.pestanas { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(max-content, 1fr); overflow-x:auto; scrollbar-width:none; background:var(--negro); border-bottom:1px solid var(--linea); }
.pestanas::-webkit-scrollbar { display:none; }
.pestanas button { padding:0 14px; white-space:nowrap; }
.pestanas button { min-height:46px; border:0; background:none; font-size:13px; font-weight:700; color:var(--suave); border-bottom:3px solid transparent; }
.pestanas button.on { color:var(--texto); border-bottom-color:var(--azul); }
.volver { display:flex; align-items:center; gap:8px; padding:10px 12px; background:var(--negro); }
.volver b { font-family:Montserrat, sans-serif; font-weight:900; font-size:18px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { min-height:36px; display:inline-flex; align-items:center; gap:6px; padding:0 12px; border-radius:18px; border:1px solid var(--borde); background:none; color:#C7D2E0; font-size:12px; font-weight:700; }
.chip.on { background:var(--texto); color:var(--fondo); border-color:var(--texto); }
.punto { width:8px; height:8px; border-radius:4px; display:inline-block; }
.meses { display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:6px; }
.mes { min-height:38px; border-radius:9px; border:0; background:var(--sup); color:#C7D2E0; font-size:12px; font-weight:700; }
.mes.on { background:var(--azul); color:var(--fondo); }
.cal { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:4px; }
.cal .dsem { text-align:center; font-size:11px; font-weight:700; color:var(--suave); padding-bottom:4px; }
.cdia { height:46px; border-radius:10px; border:0; background:transparent; color:#7A8799; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; font-size:14px; font-weight:700; }
.cdia.con { background:var(--sup); color:var(--texto); }
.cdia.hoy { outline:1.5px solid var(--azul); }
.cdia.on { background:var(--azul); color:var(--fondo); }
.cdia .ps { display:flex; gap:2px; height:5px; } .cdia .ps i { width:5px; height:5px; border-radius:3px; }
.marcador { display:grid; grid-template-columns:1fr auto 1fr; gap:8px; align-items:start; padding:14px; background:var(--negro); }
.marcador .lado { display:flex; flex-direction:column; align-items:center; gap:8px; text-align:center; font-weight:600; font-size:14px; }
.marcador .lado img, .marcador .lado .esc { width:58px; height:58px; }
.marcador .res { font-family:Montserrat, sans-serif; font-weight:900; font-size:40px; text-align:center; padding-top:6px; font-variant-numeric:tabular-nums; }
.marcador .estado { text-align:center; font-size:13px; font-weight:700; color:var(--suave); }
.marcador .estado.vivo { color:var(--vivo); }
.dato { display:flex; justify-content:space-between; gap:10px; padding:11px 0; border-top:1px solid var(--linea); font-size:13px; }
.dato span:first-child { color:var(--suave); }
.boton { min-height:46px; display:flex; align-items:center; justify-content:center; gap:8px; padding:0 18px; border-radius:10px; border:0; background:var(--azul); color:var(--fondo); font-weight:800; font-size:14px; }
.boton.yt { background:#E5332A; color:#fff; }
.boton.sec { background:none; border:1.5px solid var(--plata); color:var(--texto); }
.tabla { width:100%; border-collapse:collapse; font-size:13px; }
.tabla th { padding:9px 4px; background:var(--tarj2); color:var(--suave); font-size:11px; font-weight:700; }
.tabla td { padding:9px 4px; border-top:1px solid var(--linea); text-align:center; }
.tabla .izq { text-align:left; }
.tabla .eqt { display:flex; align-items:center; gap:8px; font-weight:600; }
.pos { display:inline-flex; width:24px; height:24px; border-radius:6px; align-items:center; justify-content:center; background:#1B2E4F; font-weight:800; font-size:12px; }
.pos.top { background:var(--azul); color:var(--fondo); }
.pts { font-family:Montserrat, sans-serif; font-weight:900; font-size:16px; }
.jug { display:grid; grid-template-columns:24px 40px 1fr auto; align-items:center; gap:10px; padding:10px 12px; border-top:1px solid var(--linea); }
.avatar { width:40px; height:40px; border-radius:20px; background:#1B2E4F; color:#C7D2E0; font-weight:800; font-size:13px; display:flex; align-items:center; justify-content:center; }
.avatar.xl { width:84px; height:84px; border-radius:42px; font-size:26px; }
.goles { font-family:Montserrat, sans-serif; font-weight:900; font-size:18px; }
.datos3 { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:8px; }
.datos3 div { background:var(--sup); border-radius:10px; padding:10px; display:flex; flex-direction:column; gap:3px; }
.datos3 small { font-size:11px; color:var(--suave); } .datos3 b { font-size:15px; }
.barra { display:grid; grid-template-columns:1fr 1fr; gap:4px; margin-top:6px; }
.barra div { height:6px; border-radius:3px; background:var(--linea); display:flex; }
.barra div:first-child { justify-content:flex-end; }
.barra i { display:block; height:6px; border-radius:3px; }
select { width:100%; min-height:44px; border-radius:10px; border:1px solid var(--borde); background:var(--tarj); color:var(--texto); padding:0 10px; font-family:inherit; font-size:14px; }
.clubes { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:8px; }
.club { min-height:52px; display:flex; align-items:center; gap:10px; padding:0 10px; border-radius:12px; border:1.5px solid var(--linea); background:var(--tarj); font-size:13px; font-weight:600; text-align:left; }
.club.on { border-color:var(--azul); }
.club .nom { flex:1; }
.nota-pie { font-size:12px; color:var(--suave); line-height:1.5; padding:0 6px; }
.carrusel { display:flex; gap:10px; overflow-x:auto; scroll-snap-type:x mandatory; scrollbar-width:none; border-radius:16px; }
.carrusel::-webkit-scrollbar { display:none; }
.carrusel .hero { flex:0 0 100%; scroll-snap-align:center; }
.puntos { display:flex; justify-content:center; gap:2px; margin-top:-4px; }
.puntos button { width:24px; height:24px; border:0; background:none; padding:0; display:flex; align-items:center; justify-content:center; }
.puntos i { display:block; width:8px; height:8px; border-radius:4px; background:#2C4470; transition:width .2s, background .2s; }
.puntos .on i { width:20px; background:var(--azul); }
.tema { display:inline-block; font-size:10px; font-weight:800; letter-spacing:.4px; text-transform:uppercase; color:#8DB8FF; }
.hero .tema { padding:3px 8px; border-radius:5px; background:rgba(5,7,13,.75); margin-left:4px; }
.tema.imp { color:#FF8A90; }
[data-club] { cursor:pointer; }
.portada { display:flex; flex-direction:column; align-items:center; gap:8px; text-align:center; padding:28px 16px 22px; background:var(--negro) center/cover no-repeat; border-bottom:1px solid var(--linea); }
.escudo-xl { width:96px; height:96px; object-fit:contain; filter:drop-shadow(0 4px 12px rgba(0,0,0,.5)); }
.camisetas { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; }
.camisetas figure { margin:0; background:var(--sup); border-radius:12px; padding:10px; text-align:center; }
.camisetas img { width:100%; height:110px; object-fit:contain; }
.camisetas figcaption { font-size:12px; font-weight:700; color:var(--suave); margin-top:6px; }
.kit { position:relative; width:100px; height:135px; margin:0 auto; transform:scale(.82); transform-origin:top center; margin-bottom:-24px; }
.kit > div { position:absolute; overflow:hidden; }
.camisetas figure:has(.kit) { background:#fff; } .camisetas figure:has(.kit) figcaption { color:#3A4658; }
.kit-svg { width:100%; height:110px; display:block; }
.camisetas svg.kit-pix { width:96px; height:96px; max-width:100%; display:block; margin:0 auto; background:#fff; border-radius:8px; }
.kit > div img { width:100%; height:100%; object-fit:fill; display:block; }
.kit.pix > div img { image-rendering:pixelated; }
.kit .la { left:0; top:0; width:31px; height:59px; } .kit .b { left:31px; top:0; width:38px; height:59px; }
.kit .ra { left:69px; top:0; width:31px; height:59px; } .kit .sh { left:0; top:59px; width:100px; height:36px; }
.kit .so { left:0; top:95px; width:100px; height:40px; }
.camisetas .aprox { display:block; font-size:10px; font-weight:600; color:#5B6B82; margin-top:2px; }
.foto-jug { width:96px; height:96px; border-radius:48px; object-fit:cover; object-position:top; background:linear-gradient(#1B2E4F, #0D1626); border:2px solid var(--azulreal); flex:none; }
.video { display:block; border-radius:16px; overflow:hidden; background:var(--tarj); border:1px solid var(--linea); }
.portada-video { position:relative; aspect-ratio:16/9; background:#000; }
.portada-video img { width:100%; height:100%; object-fit:cover; display:block; }
.cargando-video { border-radius:16px; background:linear-gradient(90deg, #0D1626, #132039, #0D1626); }
.play { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); width:68px; height:48px; border-radius:14px; background:#E5332A; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 18px rgba(0,0,0,.5); }
.info-video { padding:12px 14px; display:flex; flex-direction:column; gap:4px; }
.info-video b { font-size:14px; line-height:1.35; }
.info-video small { font-size:12px; color:var(--suave); }
.avatar.grande { width:56px; height:56px; border-radius:28px; font-size:17px; }
.avatar.con-foto { object-fit:cover; object-position:top; background:linear-gradient(#1B2E4F, #0D1626); }
.cancha { margin:12px; padding:18px 6px; border-radius:14px; display:flex; flex-direction:column; gap:16px;
  background:repeating-linear-gradient(to bottom, #1E5B36 0 44px, #1A5130 44px 88px); border:2px solid rgba(255,255,255,.25); }
.linea { display:flex; justify-content:space-around; }
.slot { display:flex; flex-direction:column; align-items:center; gap:4px; width:64px; }
.cara { width:46px; height:46px; border-radius:23px; border:2px solid #fff; background:#1B2E4F; color:#fff; font-weight:800; font-size:13px; display:flex; align-items:center; justify-content:center; object-fit:cover; object-position:top; box-shadow:0 2px 8px rgba(0,0,0,.4); }
.nom-slot { font-size:11px; font-weight:700; background:rgba(5,7,13,.75); padding:1px 6px; border-radius:4px; max-width:76px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.creditos { margin:10px 10px 0; padding:22px 12px 26px; border-top:1px solid var(--linea); display:flex; flex-direction:column; align-items:center; gap:8px; text-align:center; }
.creditos img { width:48px; height:48px; border-radius:10px; object-fit:cover; opacity:.9; }
.creditos .nombre { font-family:Montserrat, sans-serif; font-weight:900; font-size:15px; letter-spacing:.5px; color:#E6E9EE; }
.creditos .nombre span { color:var(--azul); }
.creditos p { margin:0; font-size:12px; color:var(--suave); line-height:1.6; }
.creditos b { color:var(--plata); font-weight:700; }
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <button class="icono" id="abrirMenu" aria-label="Abrir menú de ligas">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
    </button>
    <a class="marca" href="#/">
      <img src="/logo.png" alt="Escudo de Tiki Data" onerror="this.remove()">
      <div><div class="nombre">TIKI<span>DATA</span></div><div class="lema">EL FÚTBOL EN DATOS</div></div>
    </a>
  </header>
  <div id="vista"><div class="cargando">Cargando…</div></div>
  <footer class="creditos">
    <img src="/logo.png" alt="" onerror="this.remove()">
    <div class="nombre">TIKI<span>DATA</span></div>
    <p>Desarrollado por <b>Teo Gunther Crespo</b></p>
    <p>© 2026 Tiki Data · Datos de football-data.org, Highlightly y TheSportsDB</p>
  </footer>
</div>

<nav class="abajo" aria-label="Secciones"><div id="navAbajo"></div></nav>

<div class="cortina" id="cortina">
  <aside aria-label="Menú de ligas">
    <div class="cabm">
      <img src="/logo.png" alt="" width="40" height="40" style="border-radius:8px" onerror="this.remove()">
      <b>Ligas y torneos</b>
      <button class="icono" id="cerrarMenu" aria-label="Cerrar menú"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>
    <div id="menuLigas"></div>
    <p style="margin:24px 20px 0;font-size:11px;color:var(--suave);line-height:1.6">Desarrollado por <b style="color:var(--plata)">Teo Gunther Crespo</b><br>© 2026 Tiki Data</p>
  </aside>
  <button class="fondo" id="fondoMenu" aria-label="Cerrar menú"></button>
</div>

<script>
const LIGAS = __LIGAS__;
const HL = __HL__;  // torneos que vienen de Highlightly
const COLOR = { CL:'#8DB8FF', PL:'#C084FC', PD:'#FB923C', SA:'#2DD4BF', BL1:'#F87171', FL1:'#FACC15', PPL:'#22C55E', DED:'#F97316', ELC:'#A3A3A3', BSA:'#4ADE80', WC:'#E879F9', LPF:'#60A5FA', CAR:'#F472B6', LIB:'#FBBF24', SUD:'#34D399' };
const DIAS = ['Dom','Lun','Mar','Mié','Jue','Vie','Sáb'];
const DIAS_L = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
const MESES = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
const MESES_L = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hoyISO = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,10); };
const leer = (k, def) => { try { return JSON.parse(localStorage.getItem(k)) ?? def; } catch (e) { return def; } };
const guardar = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
const ICON = {
  inicio:'M4 11l8-7 8 7v9H4z', cal:'M3 5h18v16H3zM3 10h18M8 3v4M16 3v4',
  estrella:'M12 4l2.4 5 5.6.6-4.2 3.8 1.2 5.6L12 16.2 7 19l1.2-5.6L4 9.6 9.6 9z',
  jug:'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20c1.5-4 4.5-5 8-5s6.5 1 8 5', menu:'M4 7h16M4 12h16M4 17h16',
  atras:'M15 5l-7 7 7 7', sig:'M9 5l7 7-7 7'
};
const svg = (d, s=22, extra='') => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ${extra} aria-hidden="true"><path d="${d}"/></svg>`;

async function pedir(ruta) {
  const r = await fetch(ruta);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Error desconocido');
  return d;
}
function escudo(t, cls='esc') {
  const club = t && t.id ? ` data-club="${t.id}" role="link" tabindex="0" aria-label="Ver club ${esc(t.nombre)}"` : '';
  return t && t.escudo ? `<img class="${cls}" src="${esc(t.escudo)}" alt="" loading="lazy"${club}>`
    : `<span class="${cls} ph"${club}>${esc(((t && (t.sigla || t.nombre)) || '?').slice(0,3))}</span>`;
}
function iniciales(n) { return esc(String(n || '?').split(' ').filter(Boolean).slice(0,2).map(x => x[0]).join('').toUpperCase()); }
function estadoTexto(p) {
  if (p.vivo) return p.estado === 'PAUSED' ? 'ET' : (p.minuto ? p.minuto + "'" : 'Vivo');
  if (p.terminado) return 'Final';
  if (p.estado === 'POSTPONED') return 'Post.';
  if (p.estado === 'CANCELLED') return 'Susp.';
  return p.hora;
}
function filaPartido(p, mostrarLiga=false) {
  const jugado = p.gl !== null && p.gl !== undefined && p.gv !== null && p.gv !== undefined;
  const pl = jugado && p.terminado && p.gl < p.gv, pv = jugado && p.terminado && p.gv < p.gl;
  const st = mostrarLiga ? `<span style="display:flex;flex-direction:column;align-items:center;gap:3px">${esc(estadoTexto(p))}<i style="font-style:normal;font-size:9px;font-weight:800;color:${COLOR[p.liga]||'#8E9AAE'}">${esc(p.liga)}</i></span>` : esc(estadoTexto(p));
  return `<a class="fila" href="#/partido/${p.id}">
    <span class="st ${p.vivo ? 'vivo' : ''}">${st}</span>
    <span class="eq der ${pl ? 'pierde' : ''}"><span class="nom">${esc(p.local.nombre)}</span>${escudo(p.local)}</span>
    <span class="marc ${p.vivo ? 'vivo' : jugado ? 'jug' : ''}">${jugado ? p.gl + ' - ' + p.gv : 'vs'}</span>
    <span class="eq ${pv ? 'pierde' : ''}">${escudo(p.visitante)}<span class="nom">${esc(p.visitante.nombre)}</span></span>
  </a>`;
}
function error(e) { return `<div class="tarjeta aviso">No se pudo cargar.<br>${esc(e.message)}</div>`; }
function volver(titulo, destino='#/') {
  return `<div class="volver"><a class="icono" href="${destino}" aria-label="Volver">${svg(ICON.atras)}</a><b>${esc(titulo)}</b></div>`;
}

/* ---------- Menú y navegación ---------- */
function armarMenu() {
  const grupos = [['ARGENTINA', ['LPF','CAR'].filter(c => LIGAS[c])], ['SUDAMÉRICA', ['LIB','SUD','BSA'].filter(c => LIGAS[c])], ['EUROPA', ['CL','PL','PD','SA','BL1','FL1','PPL','DED','ELC']], ['SELECCIONES', ['WC']]].filter(g => g[1].length);
  let h = `<div class="grupo">
    <a class="item" href="#/">${svg(ICON.inicio,20)}Inicio</a>
    <a class="item" href="#/calendario">${svg(ICON.cal,20)}Calendario</a>
    <a class="item" href="#/jugadores">${svg(ICON.jug,20)}Goleadores y comparador</a>
    <a class="item" href="#/favoritos">${svg(ICON.estrella,20)}Mis favoritos</a></div>`;
  grupos.forEach(([n, cods]) => {
    h += `<div class="grupo"><small>${n}</small>` + cods.map(c => `<a class="item" href="#/liga/${c}"><span class="punto" style="background:${COLOR[c]}"></span>${esc(LIGAS[c][0])}</a>`).join('');
    if (n === 'ARGENTINA' || (n === 'SUDAMÉRICA' && !LIGAS.LPF)) h += ['Primera Nacional', 'Liga uruguaya', 'MLS'].map(x => `<span class="item off"><span class="punto" style="background:#475569"></span>${x}<em>PRÓXIMAMENTE</em></span>`).join('');
    h += '</div>';
  });
  $('menuLigas').innerHTML = h;
}
const abrirMenu = () => $('cortina').classList.add('abierta');
const cerrarMenu = () => $('cortina').classList.remove('abierta');
$('abrirMenu').onclick = abrirMenu; $('cerrarMenu').onclick = cerrarMenu; $('fondoMenu').onclick = cerrarMenu;
$('menuLigas').addEventListener('click', e => { if (e.target.closest('a')) cerrarMenu(); });

function navAbajo(actual) {
  const t = [['inicio','#/','Inicio',ICON.inicio], ['calendario','#/calendario','Calendario',ICON.cal],
             ['favoritos','#/favoritos','Favoritos',ICON.estrella], ['jugadores','#/jugadores','Jugadores',ICON.jug]];
  $('navAbajo').innerHTML = t.map(([k, href, l, ic]) => `<a href="${href}" class="${k === actual ? 'on' : ''}">${svg(ic)}${l}</a>`).join('')
    + `<button id="navLigas">${svg(ICON.menu)}Ligas</button>`;
  $('navLigas').onclick = abrirMenu;
}

/* ---------- Inicio ---------- */
async function vistaInicio(fecha) {
  fecha = fecha || hoyISO();
  const base = new Date(hoyISO() + 'T12:00:00');
  let dias = '';
  for (let i = -2; i <= 2; i++) {
    const d = new Date(base); d.setDate(d.getDate() + i);
    const iso = new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,10);
    dias += `<a class="dia ${iso === fecha ? 'on' : ''}" href="#/dia/${iso}"><small>${i === 0 ? 'HOY' : DIAS[d.getDay()].toUpperCase()}</small><b>${d.getDate()}</b></a>`;
  }
  $('vista').innerHTML = `<div id="vivos"></div>
    <nav class="dias" aria-label="Día">${dias}<a class="dia" href="#/calendario" aria-label="Abrir calendario" style="color:var(--azul)">${svg(ICON.cal,20)}</a></nav>
    <main><div id="favs"></div><div id="ligasHoy" style="display:flex;flex-direction:column;gap:14px"><div class="cargando">Buscando partidos…</div></div><div id="noticias"></div></main>`;
  cargarNoticias();
  const favs = leer('tiki_favs', []);
  try {
    const d = await pedir('/api/hoy?fecha=' + fecha);
    const todos = d.grupos.flatMap(g => g.partidos);
    const vivos = todos.filter(p => p.vivo);
    if (vivos.length) $('vivos').innerHTML = `<div class="vivos"><span class="tag">EN VIVO</span>` + vivos.map(p => `<a class="chipvivo" href="#/partido/${p.id}"><b>${esc(estadoTexto(p))}</b>${escudo(p.local)}<strong>${p.gl ?? 0}-${p.gv ?? 0}</strong>${escudo(p.visitante)}</a>`).join('') + '</div>';
    const idsFav = favs.map(f => f.id);
    const deFavs = todos.filter(p => idsFav.includes(p.local.id) || idsFav.includes(p.visitante.id));
    let favHTML = '';
    if (!favs.length) {
      favHTML = `<a class="tarjeta pad" href="#/favoritos" style="display:flex;align-items:center;gap:12px"><span style="color:var(--azul)">${svg(ICON.estrella,26)}</span><span style="flex:1"><b>Elegí tus clubes favoritos</b><br><small style="color:var(--suave)">Sus partidos van a aparecer primero</small></span>${svg(ICON.sig,18)}</a>`;
    } else {
      favHTML = `<section class="tarjeta"><div class="cab"><span style="color:var(--azul)">${svg(ICON.estrella,20,'fill="currentColor"')}</span><div class="t"><b>Mis favoritos</b><small>${esc(favs.map(f => f.nombre).join(' · '))}</small></div><a class="link" href="#/favoritos">Editar</a></div>`
        + (deFavs.length ? deFavs.map(p => filaPartido(p, true)).join('') : `<div id="proxFavs"><div class="aviso" style="padding:14px">Hoy no juegan. Buscando sus próximos partidos…</div></div>`) + '</section>';
    }
    $('favs').innerHTML = favHTML;
    if (favs.length && !deFavs.length) cargarProximosFavs(favs);
    $('ligasHoy').innerHTML = d.grupos.length ? d.grupos.map(g => `<section class="tarjeta">
        <a class="cab" href="#/liga/${g.liga}">${g.emblema ? `<img class="emb" src="${esc(g.emblema)}" alt="">` : ''}<div class="t"><b>${esc(g.nombre)}</b><small>${esc(g.region)}</small></div>${svg(ICON.sig,16)}</a>
        ${g.partidos.map(p => filaPartido(p)).join('')}</section>`).join('')
      : `<div class="tarjeta aviso">No hay partidos de las ligas disponibles este día.<br><a class="link" href="#/calendario">Ver el calendario</a></div>`;
  } catch (e) { $('ligasHoy').innerHTML = error(e); }
}
async function cargarProximosFavs(favs) {
  const filas = [];
  for (const f of favs.slice(0, 5)) {
    try { const d = await pedir('/api/proximos?equipo=' + f.id); const p = d.partidos.find(x => x.fecha >= hoyISO()); if (p && !filas.some(x => x.id === p.id)) filas.push(p); } catch (e) {}
  }
  filas.sort((a, b) => (a.fecha + a.hora).localeCompare(b.fecha + b.hora));
  const el = $('proxFavs'); if (!el) return;
  el.innerHTML = filas.length ? filas.map(p => {
    const d = new Date(p.fecha + 'T12:00:00');
    return `<div style="padding:8px 12px 0;font-size:11px;font-weight:700;color:var(--suave)">${DIAS[d.getDay()]} ${d.getDate()}/${d.getMonth()+1} · ${esc(p.ligaNombre)}</div>` + filaPartido(p);
  }).join('') : '<div class="aviso" style="padding:14px">No encontramos próximos partidos.</div>';
}
const TEMAS_TXT = { arg:'Fútbol argentino', asc:'Ascenso', sel:'Selección Argentina', top:'Clubes grandes y clásicos', pl:'Premier League', liga:'La Liga', fich:'Fichajes', est:'Estrellas (Messi, Cristiano y la élite)' };
async function cargarNoticias() {
  const el = $('noticias'); if (!el) return;
  const prefs = leer('tiki_prefs_noticias', []);
  try {
    const d = await pedir('/api/noticias?prefs=' + encodeURIComponent(prefs.join(',')));
    const todas = d.destacadas.length + d.noticias.length;
    const img = (n) => n.imagen ? `<img src="${esc(n.imagen)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<img src="/logo.png" alt="" style="object-fit:contain;padding:30px;opacity:.35" onerror="this.remove()">`;
    const etiqueta = (n) => n.importante ? '<span class="tema imp">IMPORTANTE</span>' : (n.tema ? `<span class="tema">${esc(n.tema)}</span>` : '');
    el.innerHTML = `<section style="display:flex;flex-direction:column;gap:12px">
      <div class="seccion"><h2>Noticias</h2><button class="link" id="btnPers">${prefs.length ? 'Mis temas (' + prefs.length + ')' : 'Personalizar'}</button></div>
      <div id="panelPers" class="tarjeta pad" hidden>
        <b style="font-size:14px">¿Qué noticias querés ver?</b>
        <p class="nota-pie" style="margin:6px 0 12px;padding:0">Elegí uno o varios temas. Las noticias más importantes aparecen siempre.</p>
        <div class="chips">${Object.entries(TEMAS_TXT).map(([k, t]) => `<button class="chip ${prefs.includes(k) ? 'on' : ''}" data-tema="${k}" aria-pressed="${prefs.includes(k)}">${esc(t)}</button>`).join('')}</div>
        <div style="display:flex;gap:8px;margin-top:14px"><button class="boton" id="guardarPers" style="flex:1">Guardar</button><button class="boton sec" id="borrarPers">Lo recomendado</button></div>
      </div>
      ${todas ? '' : '<div class="tarjeta aviso">No encontramos noticias de esos temas ahora. Probá con otros.</div>'}
      ${d.destacadas.length ? `<div class="carrusel" id="carrusel">${d.destacadas.map(n => `<a class="hero" href="${esc(n.enlace)}" target="_blank" rel="noopener">${img(n)}
        <div class="capa"><span class="fuente">${esc(n.fuente)}</span> ${etiqueta(n)}<div class="tit">${esc(n.titulo)}</div><small>${esc(n.cuando)} · Leer en ${esc(n.fuente)}</small></div></a>`).join('')}</div>
      ${d.destacadas.length > 1 ? `<div class="puntos" role="tablist" aria-label="Noticias destacadas">${d.destacadas.map((_, i) => `<button role="tab" aria-label="Noticia ${i + 1}" data-i="${i}" class="${i === 0 ? 'on' : ''}"><i></i></button>`).join('')}</div>` : ''}` : ''}
      <div class="grilla">${d.noticias.slice(0, 16).map(n => `<a class="nota" href="${esc(n.enlace)}" target="_blank" rel="noopener">
        <div class="img">${img(n)}<span class="fuente">${esc(n.fuente)}</span></div>
        <div class="txt">${n.importante || n.tema ? `<span class="tema ${n.importante ? 'imp' : ''}">${esc(n.importante ? 'IMPORTANTE' : n.tema)}</span>` : ''}${esc(n.titulo)}<small>${esc(n.cuando)}</small></div></a>`).join('')}</div></section>`;
    const car = $('carrusel');
    if (car) {
      const puntos = [...el.querySelectorAll('.puntos button')];
      const paso = () => car.clientWidth + 10;
      car.addEventListener('scroll', () => {
        const i = Math.round(car.scrollLeft / paso());
        puntos.forEach((b, j) => { b.classList.toggle('on', i === j); b.setAttribute('aria-selected', i === j); });
      }, { passive: true });
      puntos.forEach(b => b.onclick = () => car.scrollTo({ left: +b.dataset.i * paso(), behavior: 'smooth' }));
    }
    const elegidos = new Set(prefs);
    $('btnPers').onclick = () => { $('panelPers').hidden = !$('panelPers').hidden; };
    el.querySelectorAll('[data-tema]').forEach(b => b.onclick = () => {
      const k = b.dataset.tema; elegidos.has(k) ? elegidos.delete(k) : elegidos.add(k);
      b.classList.toggle('on', elegidos.has(k)); b.setAttribute('aria-pressed', elegidos.has(k));
    });
    $('guardarPers').onclick = () => { guardar('tiki_prefs_noticias', [...elegidos]); cargarNoticias(); };
    $('borrarPers').onclick = () => { guardar('tiki_prefs_noticias', []); cargarNoticias(); };
  } catch (e) { el.innerHTML = ''; }
}

async function cargarResumenYT(id) {
  const el = $('resumenYT'); if (!el) return;
  try {
    const r = await pedir('/api/resumen?id=' + id);
    const v = r.video;
    if (v) {
      el.innerHTML = `<a class="video" href="${esc(v.enlace)}" target="_blank" rel="noopener" aria-label="Ver en YouTube: ${esc(v.titulo)}">
        <div class="portada-video"><img src="${esc(v.portada)}" alt="" loading="lazy" referrerpolicy="no-referrer">
          <span class="play">${svg('M8 5v14l11-7z',26,'fill="#fff" stroke="none"')}</span></div>
        <div class="info-video"><b>${esc(v.titulo)}</b><small>${esc(v.canal)} · Ver en YouTube ↗</small></div></a>`;
    } else {
      const txt = r.motivo === 'no_termino' ? 'El resumen aparece cuando termina el partido.' : 'Todavía no encontramos el video del resumen.';
      el.innerHTML = `<a class="boton yt" href="${esc(r.busqueda)}" target="_blank" rel="noopener">${svg('M8 5v14l11-7z',18,'fill="currentColor" stroke="none"')}Buscar el resumen en YouTube</a><p class="nota-pie" style="margin:6px 0 0">${txt}</p>`;
    }
  } catch (e) { el.innerHTML = ''; }
}

/* ---------- Partido ---------- */
async function vistaPartido(id) {
  $('vista').innerHTML = volver('Partido') + '<div class="cargando">Cargando partido…</div>';
  let p;
  try { p = await pedir('/api/partido?id=' + id); } catch (e) { $('vista').innerHTML = volver('Partido') + `<main>${error(e)}</main>`; return; }
  const jugado = p.gl !== null && p.gl !== undefined;
  const d = new Date(p.fecha + 'T12:00:00');
  const fechaTxt = `${DIAS_L[d.getDay()]} ${d.getDate()} de ${MESES_L[d.getMonth()]} · ${p.hora}`;
  const yt = 'https://www.youtube.com/results?search_query=' + encodeURIComponent(`${p.local.nombre} vs ${p.visitante.nombre} resumen ${p.fecha.slice(0,4)}`);
  let estado = p.vivo ? `${estadoTexto(p)} · En vivo` : p.terminado ? 'Final' : fechaTxt;
  const esHL = p.fuente === 'hl';
  const pest = esHL ? ['Resumen', 'Goles', 'Formación', 'Estadísticas', 'Minuto a minuto'] : ['Resumen', 'Goles', 'Historial'];
  $('vista').innerHTML = `<div class="volver"><a class="icono" href="javascript:history.back()" aria-label="Volver">${svg(ICON.atras)}</a>
      <a href="#/liga/${p.liga}" style="flex:1;display:flex;flex-direction:column"><b style="font-size:15px">${esc(p.ligaNombre)}</b><small style="color:var(--suave);font-size:12px">${p.jornada ? 'Fecha ' + p.jornada : esc((p.fase || '').replaceAll('_',' ').toLowerCase())}</small></a></div>
    <section class="marcador">
      <div class="lado">${escudo(p.local)}${esc(p.local.nombre)}</div>
      <div><div class="res">${jugado ? p.gl + ' - ' + p.gv : 'vs'}</div><div class="estado ${p.vivo ? 'vivo' : ''}">${esc(estado)}</div></div>
      <div class="lado">${escudo(p.visitante)}${esc(p.visitante.nombre)}</div>
    </section>
    <div class="pestanas" role="tablist">${pest.map((t, i) => `<button role="tab" data-p="${i}" class="${i === 0 ? 'on' : ''}">${t}</button>`).join('')}</div>
    <main id="panel"></main>`;
  const paneles = [
    () => `<div id="resumenYT"><div class="portada-video cargando-video"></div></div>
      <section class="tarjeta pad"><h2 style="margin:0 0 4px;font-size:15px">Información</h2>
        <div class="dato"><span>Fecha</span><span>${esc(fechaTxt)}</span></div>
        ${p.medio && p.medio[0] !== null && p.medio[0] !== undefined ? `<div class="dato"><span>Primer tiempo</span><span>${p.medio[0]} - ${p.medio[1]}</span></div>` : ''}
        ${p.estadio ? `<div class="dato"><span>Estadio</span><span>${esc(p.estadio)}</span></div>` : ''}
        ${p.arbitro ? `<div class="dato"><span>Árbitro</span><span>${esc(p.arbitro)}</span></div>` : ''}
      </section>
      ${esHL ? (p.vivo ? '<p class="nota-pie">Se actualiza solo cada pocos minutos mientras se juega.</p>' : '') : '<p class="nota-pie">El minuto a minuto, las formaciones y las estadísticas del partido no vienen en el plan gratuito de la API. Se pueden sumar con una API más completa.</p>'}`,
    () => p.goles.length ? `<section class="tarjeta pad">${p.goles.map(g => `<div class="dato"><span style="color:var(--texto);font-weight:700">${g.minuto ? g.minuto + "'" : ''}</span><span style="flex:1">${esc(g.jugador)}${g.tipo === 'OWN' ? ' (en contra)' : g.tipo === 'PENALTY' ? ' (penal)' : ''}${g.asistencia ? `<br><small style="color:var(--suave)">Asistencia: ${esc(g.asistencia)}</small>` : ''}</span><span>${esc(g.equipo === p.local.id ? p.local.nombre : p.visitante.nombre)}</span></div>`).join('')}</section>`
      : `<div class="tarjeta aviso">${jugado ? (esHL ? 'Sin goles por ahora.' : 'El plan gratuito de la API no trae quién hizo los goles de este partido.') : 'Todavía no se jugó.'}</div>`,
    ...(esHL ? [() => panelFormacion(p), () => '<div id="stats"><div class="cargando">Cargando estadísticas…</div></div>', () => panelMinuto(p)] : [() => {
      const h = p.historial;
      if (!h || !h.total) return '<div class="tarjeta aviso">No hay historial entre estos equipos.</div>';
      return `<section class="tarjeta pad"><h2 style="margin:0 0 10px;font-size:15px">Últimos ${h.total} enfrentamientos</h2>
        <div class="datos3"><div><small>Ganó ${esc(p.local.nombre)}</small><b>${h.local ?? 0}</b></div><div><small>Empates</small><b>${h.empates ?? 0}</b></div><div><small>Ganó ${esc(p.visitante.nombre)}</small><b>${h.visitante ?? 0}</b></div></div></section>
        <section class="tarjeta">${h.partidos.map(x => filaPartido(x, true)).join('')}</section>`;
    }])
  ];
  const mostrar = (i) => { $('panel').innerHTML = paneles[i](); document.querySelectorAll('.pestanas button').forEach(b => b.classList.toggle('on', +b.dataset.p === i)); if (i === 0) cargarResumenYT(p.id); if (esHL && i === 2) cargarFotos($('panel')); if (esHL && i === 3) cargarStats(p); };
  document.querySelectorAll('.pestanas button').forEach(b => b.onclick = () => mostrar(+b.dataset.p));
  mostrar(0);
}

const lado = { local: true };
function panelFormacion(p) {
  const a = p.alineacion;
  if (!a) return `<div class="tarjeta aviso">${p.terminado || p.vivo ? 'No hay formaciones para este partido.' : 'Las formaciones aparecen cerca de una hora antes del partido.'}</div>`;
  const pintar = (eq, t) => {
    if (!eq || !eq.filas.length) return '<div class="aviso">Sin datos.</div>';
    const filas = eq.filas.slice().reverse();
    return `<div class="cancha">${filas.map(f => `<div class="linea">${f.map(j => `<div class="slot">${caraHTML(j, 'cara')}<span class="nom-slot">${j.numero ? j.numero + ' ' : ''}${esc(String(j.nombre).split(' ').slice(-1)[0])}</span></div>`).join('')}</div>`).join('')}</div>
      ${eq.suplentes.length ? `<div style="padding:0 12px 12px"><b style="font-size:13px">Suplentes</b><div class="chips" style="margin-top:8px">${eq.suplentes.map(j => `<span class="chip" style="cursor:default">${j.numero ? j.numero + ' · ' : ''}${esc(j.nombre)}</span>`).join('')}</div></div>` : ''}`;
  };
  const html = (k) => `<section class="tarjeta"><div class="chips" style="padding:10px 12px 0">
      <button class="chip ${k === 'local' ? 'on' : ''}" data-lado="local">${esc(p.local.nombre)}${a.local.formacion ? ' · ' + esc(a.local.formacion) : ''}</button>
      <button class="chip ${k === 'visitante' ? 'on' : ''}" data-lado="visitante">${esc(p.visitante.nombre)}${a.visitante.formacion ? ' · ' + esc(a.visitante.formacion) : ''}</button></div>
    ${pintar(a[k], k === 'local' ? p.local : p.visitante)}</section>`;
  setTimeout(() => document.querySelectorAll('[data-lado]').forEach(b => b.onclick = () => { $('panel').innerHTML = html(b.dataset.lado); panelFormacionEventos(html); cargarFotos($('panel')); }), 0);
  return html('local');
}
function panelFormacionEventos(html) { document.querySelectorAll('[data-lado]').forEach(b => b.onclick = () => { $('panel').innerHTML = html(b.dataset.lado); panelFormacionEventos(html); cargarFotos($('panel')); }); }
const STATS_ES = { 'Ball possession':'Posesión', 'Possession':'Posesión', 'Total shots':'Remates', 'Shots on target':'Remates al arco', 'Shots off target':'Remates afuera', 'Blocked shots':'Remates bloqueados', 'Corner kicks':'Córners', 'Corners':'Córners', 'Fouls':'Faltas', 'Offsides':'Offsides', 'Yellow cards':'Amarillas', 'Red cards':'Rojas', 'Goalkeeper saves':'Atajadas', 'Total passes':'Pases', 'Passes accurate':'Pases correctos', 'Expected goals':'Goles esperados (xG)' };
async function cargarStats(p) {
  const el = $('stats'); if (!el) return;
  try {
    const d = await pedir('/api/estadisticas?id=' + p.id);
    if (!d.estadisticas.length) { el.innerHTML = '<div class="tarjeta aviso">Todavía no hay estadísticas de este partido.</div>'; return; }
    const num = (v) => parseFloat(String(v ?? '0').replace('%', '').replace(',', '.')) || 0;
    el.innerHTML = `<section class="tarjeta pad"><div style="display:flex;justify-content:space-between;font-size:12px;font-weight:700;color:var(--suave);margin-bottom:6px"><span>${esc(p.local.nombre)}</span><span>${esc(p.visitante.nombre)}</span></div>
      ${d.estadisticas.map(s => { const a = num(s.local), b = num(s.visitante), t = (a + b) || 1; return `<div style="padding:10px 0;border-top:1px solid var(--linea)">
        <div style="display:grid;grid-template-columns:60px 1fr 60px;font-size:14px"><b>${esc(s.local ?? '-')}</b><span style="text-align:center;font-size:12px;color:var(--suave)">${esc(STATS_ES[s.nombre] || s.nombre)}</span><b style="text-align:right">${esc(s.visitante ?? '-')}</b></div>
        <div class="barra"><div><i style="width:${Math.round(a / t * 100)}%;background:var(--plata)"></i></div><div><i style="width:${Math.round(b / t * 100)}%;background:#FF9F43"></i></div></div></div>`; }).join('')}</section>`;
  } catch (e) { el.innerHTML = error(e); }
}
const EVENTO = { 'Goal':['⚽','Gol'], 'Own Goal':['⚽','Gol en contra'], 'Penalty':['⚽','Gol de penal'], 'Missed Penalty':['✕','Penal errado'], 'Yellow Card':['▮','Amarilla'], 'Red Card':['▮','Roja'], 'Substitution':['⇄','Cambio'], 'VAR Goal Confirmed':['VAR','Gol confirmado'], 'VAR Goal Cancelled':['VAR','Gol anulado'], 'VAR Goal Cancelled - Offside':['VAR','Gol anulado por offside'], 'VAR Penalty':['VAR','Penal por VAR'], 'VAR Penalty Cancelled':['VAR','Penal anulado'] };
function panelMinuto(p) {
  if (!p.eventos || !p.eventos.length) return `<div class="tarjeta aviso">${p.vivo || p.terminado ? 'Todavía no hay jugadas importantes.' : 'El partido no empezó.'}</div>`;
  const lista = p.eventos.slice().reverse();
  return `<section class="tarjeta pad">${lista.map(e => { const [ic, txt] = EVENTO[e.tipo] || ['•', e.tipo]; const eq = e.equipo === p.local.id ? p.local.nombre : p.visitante.nombre;
    const color = e.tipo === 'Yellow Card' ? '#F5C451' : e.tipo === 'Red Card' ? '#FF5A5F' : 'var(--texto)';
    return `<div class="dato" style="align-items:flex-start"><b style="min-width:40px;color:var(--suave)">${esc(e.minuto)}'</b>
      <span style="min-width:30px;color:${color};font-weight:800">${ic}</span>
      <span style="flex:1"><b>${txt}</b> · ${esc(eq)}<br><small style="color:#C7D2E0">${esc(e.jugador)}${e.tipo === 'Substitution' && e.sale ? ' entra por ' + esc(e.sale) : ''}${e.asistencia ? ' · asistencia de ' + esc(e.asistencia) : ''}</small></span></div>`; }).join('')}</section>`;
}

/* ---------- Liga: partidos por fecha, tabla y goleadores ---------- */
async function vistaLiga(cod, pestana) {
  pestana = pestana || 'partidos';
  $('vista').innerHTML = volver(LIGAS[cod][0]) + `<div class="pestanas">${['partidos','tabla','goleadores'].map(t => `<button data-t="${t}" class="${t === pestana ? 'on' : ''}">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div><main id="panel"><div class="cargando">Cargando…</div></main>`;
  document.querySelectorAll('.pestanas button').forEach(b => b.onclick = () => { location.hash = `#/liga/${cod}/${b.dataset.t}`; });
  try {
    if (pestana === 'tabla') {
      const d = await pedir('/api/tabla?liga=' + cod);
      $('panel').innerHTML = d.grupos.length ? d.grupos.map(g => `<section class="tarjeta">${g.nombre ? `<div class="cab"><div class="t"><b>${esc(g.nombre)}</b></div></div>` : ''}
        <table class="tabla"><thead><tr><th>#</th><th class="izq">Equipo</th><th>PJ</th><th>G</th><th>E</th><th>P</th><th>DG</th><th>PTS</th></tr></thead><tbody>
        ${g.filas.map(f => `<tr><td><span class="pos ${f.pos <= 4 ? 'top' : ''}">${f.pos}</span></td><td class="izq"><span class="eqt">${escudo(f.equipo)}<span class="nom">${esc(f.equipo.nombre)}</span></span></td><td>${f.pj}</td><td>${f.g}</td><td>${f.e}</td><td>${f.p}</td><td>${f.dg > 0 ? '+' : ''}${f.dg}</td><td class="pts">${f.pts}</td></tr>`).join('')}
        </tbody></table></section>`).join('') : '<div class="tarjeta aviso">No hay tabla para este torneo.</div>';
    } else if (pestana === 'goleadores') {
      const d = await pedir('/api/goleadores?liga=' + cod);
      $('panel').innerHTML = listaGoleadores(d, cod);
      cargarFotos($('panel'));
    } else {
      const d = await pedir('/api/calendario?liga=' + cod);
      const porJornada = {};
      d.partidos.forEach(p => { const k = p.jornada || p.fase || '-'; (porJornada[k] ||= []).push(p); });
      const claves = Object.keys(porJornada);
      let actual = claves.find(k => porJornada[k].some(p => !p.terminado)) || claves[claves.length - 1];
      const dibujar = () => {
        const i = claves.indexOf(actual);
        const nombre = /^\d+$/.test(actual) ? 'Fecha ' + actual : String(actual).replaceAll('_',' ').toLowerCase();
        $('panel').innerHTML = `<section class="tarjeta"><div class="cab">
          <button class="icono" id="jAnt" aria-label="Fecha anterior" ${i <= 0 ? 'disabled' : ''}>${svg(ICON.atras,18)}</button>
          <div class="t" style="align-items:center"><b>${esc(nombre)}</b></div>
          <button class="icono" id="jSig" aria-label="Fecha siguiente" ${i >= claves.length - 1 ? 'disabled' : ''}>${svg(ICON.sig,18)}</button></div>
          ${porJornada[actual].map(p => { const dd = new Date(p.fecha + 'T12:00:00'); return `<div style="padding:8px 12px 0;font-size:11px;font-weight:700;color:var(--suave)">${DIAS[dd.getDay()]} ${dd.getDate()}/${dd.getMonth()+1}</div>` + filaPartido(p); }).join('')}</section>`;
        $('jAnt').onclick = () => { actual = claves[i - 1]; dibujar(); };
        $('jSig').onclick = () => { actual = claves[i + 1]; dibujar(); };
      };
      dibujar();
    }
  } catch (e) { $('panel').innerHTML = error(e); }
}

/* ---------- Calendario de todo el año ---------- */
const cal = { liga: 'ALL', mes: new Date().getMonth(), anio: new Date().getFullYear(), dia: new Date().getDate(), datos: {} };
async function vistaCalendario() {
  $('vista').innerHTML = volver('Calendario') + `<main id="panel"><div class="cargando">Cargando el calendario de todas las ligas…<br><small>La primera vez puede tardar hasta un minuto.</small></div></main>`;
  const cods = Object.keys(LIGAS).filter(c => !HL.includes(c));
  await Promise.all(cods.map(async c => { if (!cal.datos[c]) { try { cal.datos[c] = (await pedir('/api/calendario?liga=' + c)).partidos; } catch (e) { cal.datos[c] = []; } } }));
  dibujarCalendario();
}
function dibujarCalendario() {
  if (cal.liga !== 'ALL' && !cal.datos[cal.liga]) {
    $('panel').innerHTML = '<div class="cargando">Cargando el calendario…</div>';
    pedir('/api/calendario?liga=' + cal.liga).then(d => { cal.datos[cal.liga] = d.partidos; }).catch(() => { cal.datos[cal.liga] = []; }).finally(dibujarCalendario);
    return;
  }
  const activas = cal.liga === 'ALL' ? Object.keys(LIGAS).filter(c => cal.datos[c]) : [cal.liga];
  const porDia = {};
  activas.forEach(c => (cal.datos[c] || []).forEach(p => { (porDia[p.fecha] ||= []).push(p); }));
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (d) => `${cal.anio}-${pad(cal.mes + 1)}-${pad(d)}`;
  const primero = (new Date(cal.anio, cal.mes, 1).getDay() + 6) % 7;
  const total = new Date(cal.anio, cal.mes + 1, 0).getDate();
  let celdas = ['L','M','M','J','V','S','D'].map(x => `<span class="dsem">${x}</span>`).join('');
  for (let i = 0; i < primero; i++) celdas += '<span></span>';
  for (let d = 1; d <= total; d++) {
    const ps = porDia[iso(d)] || [];
    const ligasDia = [...new Set(ps.map(p => p.liga))].slice(0, 3);
    const esHoy = iso(d) === hoyISO();
    celdas += `<button class="cdia ${ps.length ? 'con' : ''} ${esHoy ? 'hoy' : ''} ${d === cal.dia ? 'on' : ''}" data-d="${d}" aria-label="${d} de ${MESES_L[cal.mes]}${ps.length ? ', ' + ps.length + ' partidos' : ''}">${d}<span class="ps">${ligasDia.map(l => `<i style="background:${d === cal.dia ? '#070B14' : COLOR[l]}"></i>`).join('')}</span></button>`;
  }
  const delDia = (porDia[iso(cal.dia)] || []).sort((a, b) => a.hora.localeCompare(b.hora));
  const fd = new Date(cal.anio, cal.mes, cal.dia);
  $('panel').innerHTML = `<div class="chips">${[['ALL','Todas']].concat(Object.keys(LIGAS).map(c => [c, LIGAS[c][0]])).map(([c, n]) => `<button class="chip ${cal.liga === c ? 'on' : ''}" data-l="${c}">${c !== 'ALL' ? `<span class="punto" style="background:${COLOR[c]}"></span>` : ''}${esc(n)}</button>`).join('')}</div>
    <div class="meses">${MESES.map((m, i) => `<button class="mes ${i === cal.mes ? 'on' : ''}" data-m="${i}">${m}</button>`).join('')}</div>
    <section class="tarjeta pad">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <button class="icono" id="mAnt" aria-label="Mes anterior">${svg(ICON.atras,18)}</button>
        <b class="titulo" style="font-size:17px">${MESES_L[cal.mes][0].toUpperCase() + MESES_L[cal.mes].slice(1)} ${cal.anio}</b>
        <button class="icono" id="mSig" aria-label="Mes siguiente">${svg(ICON.sig,18)}</button></div>
      <div class="cal">${celdas}</div></section>
    <section class="tarjeta"><div class="cab"><div class="t"><b>${DIAS_L[fd.getDay()][0].toUpperCase() + DIAS_L[fd.getDay()].slice(1)} ${cal.dia} de ${MESES_L[cal.mes]}</b><small>${delDia.length ? delDia.length + ' partidos' : 'Sin partidos'}</small></div></div>
      ${delDia.length ? delDia.map(p => filaPartido(p, true)).join('') : '<div class="aviso">No hay partidos este día.</div>'}</section>`;
  document.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { cal.liga = b.dataset.l; dibujarCalendario(); });
  document.querySelectorAll('[data-m]').forEach(b => b.onclick = () => { cal.mes = +b.dataset.m; cal.dia = 1; dibujarCalendario(); });
  document.querySelectorAll('[data-d]').forEach(b => b.onclick = () => { cal.dia = +b.dataset.d; dibujarCalendario(); });
  $('mAnt').onclick = () => { cal.mes--; if (cal.mes < 0) { cal.mes = 11; cal.anio--; } cal.dia = 1; dibujarCalendario(); };
  $('mSig').onclick = () => { cal.mes++; if (cal.mes > 11) { cal.mes = 0; cal.anio++; } cal.dia = 1; dibujarCalendario(); };
}

/* ---------- Jugadores: goleadores, perfil y comparador ---------- */
const cacheGol = {};
async function goleadores(cod) { if (!cacheGol[cod]) cacheGol[cod] = await pedir('/api/goleadores?liga=' + cod); return cacheGol[cod]; }
function listaGoleadores(d, cod) {
  cacheGol[cod] = d;
  if (!d.goleadores.length) return `<div class="tarjeta aviso">${HL.includes(cod) ? 'La tabla de goleadores de este torneo llega más adelante.' : 'Todavía no hay goleadores.'}</div>`;
  return `<section class="tarjeta">${d.goleadores.map((j, i) => `<a class="jug" href="#/jugador/${cod}/${j.id}">
    <span class="pos ${i < 3 ? 'top' : ''}">${i + 1}</span>${caraHTML(j)}
    <span style="min-width:0"><b class="nom" style="display:block;font-size:14px">${esc(j.nombre)}</b><small style="color:var(--suave);display:flex;align-items:center;gap:6px">${escudo(j.equipo)}${esc(j.equipo.nombre)}</small></span>
    <span style="text-align:right"><span class="goles">${j.goles}</span><br><small style="color:var(--suave)">${j.asistencias} asist.</small></span></a>`).join('')}</section>`;
}
let ligaJug = 'PD';
async function vistaJugadores() {
  $('vista').innerHTML = volver('Goleadores') + `<main><div class="chips">${Object.keys(LIGAS).filter(c => !HL.includes(c)).map(c => `<button class="chip ${c === ligaJug ? 'on' : ''}" data-l="${c}">${esc(LIGAS[c][0])}</button>`).join('')}</div>
    <a class="boton sec" href="#/comparar">Comparar dos jugadores</a><div id="lista"><div class="cargando">Cargando…</div></div></main>`;
  document.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { ligaJug = b.dataset.l; vistaJugadores(); });
  try { $('lista').innerHTML = listaGoleadores(await goleadores(ligaJug), ligaJug); cargarFotos($('lista')); } catch (e) { $('lista').innerHTML = error(e); }
}
function edad(n) { if (!n) return '-'; const d = new Date(n), h = new Date(); let a = h.getFullYear() - d.getFullYear(); if (h < new Date(h.getFullYear(), d.getMonth(), d.getDate())) a--; return a + ' años'; }
const POS = { Offence:'Delantero', Midfield:'Mediocampista', Defence:'Defensor', Goalkeeper:'Arquero' };
async function vistaJugador(cod, id) {
  $('vista').innerHTML = volver('Jugador', '#/jugadores') + '<div class="cargando">Cargando…</div>';
  let d; try { d = await goleadores(cod); } catch (e) { $('vista').innerHTML = volver('Jugador') + `<main>${error(e)}</main>`; return; }
  const j = d.goleadores.find(x => String(x.id) === String(id));
  if (!j) { $('vista').innerHTML = volver('Jugador') + '<main><div class="tarjeta aviso">No encontramos al jugador.</div></main>'; return; }
  const gpp = j.partidos ? (j.goles / j.partidos).toFixed(2).replace('.', ',') : '-';
  $('vista').innerHTML = volver('Jugador', '#/jugadores') + `<section style="background:var(--negro);padding:6px 16px 18px;border-bottom:1px solid var(--linea);display:flex;flex-direction:column;gap:16px">
      <div style="display:flex;align-items:center;gap:14px"><span class="avatar xl">${iniciales(j.nombre)}</span>
        <div style="display:flex;flex-direction:column;gap:6px"><b class="titulo" style="font-size:22px">${esc(j.nombre)}</b>
        <span style="display:flex;align-items:center;gap:8px;font-weight:600">${escudo(j.equipo)}${esc(j.equipo.nombre)}</span></div></div>
      <div class="datos3"><div><small>Nacionalidad</small><b>${esc(j.nacionalidad || '-')}</b></div><div><small>Edad</small><b>${edad(j.nacimiento)}</b></div><div><small>Posición</small><b>${esc(POS[j.posicion] || j.posicion || '-')}</b></div></div></section>
    <main><section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">${esc(d.nombre)} · temporada actual</h2>
      <div class="datos3"><div><small>Goles</small><b>${j.goles}</b></div><div><small>Asistencias</small><b>${j.asistencias}</b></div><div><small>Partidos</small><b>${j.partidos}</b></div>
      <div><small>Penales</small><b>${j.penales}</b></div><div><small>Goles por partido</small><b>${gpp}</b></div><div><small>Ranking</small><b>${d.goleadores.indexOf(j) + 1}º</b></div></div></section>
      <a class="boton" href="#/comparar/${cod}/${j.id}">Comparar con otro jugador</a>
      <div id="extra" style="display:flex;flex-direction:column;gap:14px"><div class="cargando">Buscando foto y trayectoria…</div></div></main>`;
  bloqueJugadorExtra(j.nombre, 'extra');
}
async function vistaComparar(cod, idA) {
  cod = cod || ligaJug;
  $('vista').innerHTML = volver('Comparar jugadores', '#/jugadores') + '<div class="cargando">Cargando…</div>';
  let d; try { d = await goleadores(cod); } catch (e) { $('vista').innerHTML = volver('Comparar') + `<main>${error(e)}</main>`; return; }
  const lista = d.goleadores;
  if (lista.length < 2) { $('vista').innerHTML = volver('Comparar') + '<main><div class="tarjeta aviso">No hay suficientes jugadores.</div></main>'; return; }
  let a = lista.find(x => String(x.id) === String(idA)) || lista[0];
  let b = lista.find(x => x !== a);
  const opciones = (sel) => lista.map(x => `<option value="${x.id}" ${x === sel ? 'selected' : ''}>${esc(x.nombre)} (${esc(x.equipo.nombre)})</option>`).join('');
  const dibujar = () => {
    const filas = [['Goles','goles'], ['Asistencias','asistencias'], ['Partidos','partidos'], ['Penales','penales'], ['Goles por partido', null]].map(([n, k]) => {
      const va = k ? a[k] : (a.partidos ? a.goles / a.partidos : 0), vb = k ? b[k] : (b.partidos ? b.goles / b.partidos : 0);
      const t = (va + vb) || 1, f = (v) => k ? v : v.toFixed(2).replace('.', ',');
      return `<div style="padding:12px 0;border-bottom:1px solid var(--linea)"><div style="display:grid;grid-template-columns:60px 1fr 60px;font-size:14px">
        <b style="${va >= vb ? '' : 'font-weight:500;color:var(--suave)'}">${f(va)}</b><span style="text-align:center;font-size:12px;color:var(--suave)">${n}</span><b style="text-align:right;${vb >= va ? '' : 'font-weight:500;color:var(--suave)'}">${f(vb)}</b></div>
        <div class="barra"><div><i style="width:${Math.round(va / t * 100)}%;background:var(--plata)"></i></div><div><i style="width:${Math.round(vb / t * 100)}%;background:#FF9F43"></i></div></div></div>`;
    }).join('');
    $('cuerpo').innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      ${[a, b].map((x, i) => `<div class="tarjeta pad" style="text-align:center;border-top:3px solid ${i ? '#FF9F43' : 'var(--plata)'};display:flex;flex-direction:column;align-items:center;gap:6px">${caraHTML(x, 'avatar grande')}<b style="font-size:14px">${esc(x.nombre)}</b><small style="color:var(--suave);display:flex;gap:6px;align-items:center">${escudo(x.equipo)}${esc(x.equipo.nombre)}</small></div>`).join('')}</div>
      <section class="tarjeta pad">${filas}</section>`;
    cargarFotos($('cuerpo'));
  };
  $('vista').innerHTML = volver('Comparar jugadores', '#/jugadores') + `<main>
    <div class="chips">${Object.keys(LIGAS).filter(c => !HL.includes(c)).map(c => `<a class="chip ${c === cod ? 'on' : ''}" href="#/comparar/${c}">${esc(LIGAS[c][0])}</a>`).join('')}</div>
    <label style="font-size:12px;color:var(--suave)">Jugador 1<select id="selA">${opciones(a)}</select></label>
    <label style="font-size:12px;color:var(--suave)">Jugador 2<select id="selB">${opciones(b)}</select></label>
    <div id="cuerpo" style="display:flex;flex-direction:column;gap:12px"></div></main>`;
  $('selA').onchange = (e) => { a = lista.find(x => String(x.id) === e.target.value); dibujar(); };
  $('selB').onchange = (e) => { b = lista.find(x => String(x.id) === e.target.value); dibujar(); };
  dibujar();
}

/* ---------- Favoritos ---------- */
let ligaFav = 'PD';
async function vistaFavoritos() {
  const favs = leer('tiki_favs', []);
  $('vista').innerHTML = volver('Mis clubes favoritos') + `<main>
    <p class="nota-pie" style="margin:0">Tocá la estrella de los clubes que querés seguir. Sus partidos aparecen primero en el Inicio. Se guardan en este dispositivo.</p>
    ${favs.length ? `<section class="tarjeta pad"><b style="font-size:14px">Elegidos (${favs.length})</b><div class="chips" style="margin-top:10px">${favs.map(f => `<button class="chip on" data-quitar="${f.id}">${escudo(f)}${esc(f.nombre)} ✕</button>`).join('')}</div></section>` : ''}
    <div class="chips">${Object.keys(LIGAS).filter(c => !['CL', 'WC', 'CAR', 'LIB', 'SUD'].includes(c)).map(c => `<button class="chip ${c === ligaFav ? 'on' : ''}" data-l="${c}">${esc(LIGAS[c][0])}</button>`).join('')}</div>
    <div id="clubes"><div class="cargando">Cargando clubes…</div></div></main>`;
  document.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { ligaFav = b.dataset.l; vistaFavoritos(); });
  document.querySelectorAll('[data-quitar]').forEach(b => b.onclick = () => { guardar('tiki_favs', favs.filter(f => String(f.id) !== b.dataset.quitar)); vistaFavoritos(); });
  try {
    const d = await pedir('/api/equipos?liga=' + ligaFav);
    const ids = favs.map(f => f.id);
    $('clubes').innerHTML = `<div class="clubes">${d.equipos.map(t => `<button class="club ${ids.includes(t.id) ? 'on' : ''}" data-id="${t.id}" aria-pressed="${ids.includes(t.id)}">${escudo(t)}<span class="nom">${esc(t.nombre)}</span><span style="color:${ids.includes(t.id) ? 'var(--azul)' : '#5B6B82'}">${svg(ICON.estrella,18, ids.includes(t.id) ? 'fill="currentColor"' : '')}</span></button>`).join('')}</div>`;
    document.querySelectorAll('.club').forEach(b => b.onclick = () => {
      const t = d.equipos.find(x => String(x.id) === b.dataset.id);
      const ahora = leer('tiki_favs', []);
      guardar('tiki_favs', ahora.some(f => f.id === t.id) ? ahora.filter(f => f.id !== t.id) : ahora.concat([t]));
      vistaFavoritos();
    });
  } catch (e) { $('clubes').innerHTML = error(e); }
}

/* ---------- Ficha del club (al tocar un escudo) ---------- */
document.addEventListener('click', (e) => {
  const c = e.target.closest('[data-club]');
  if (c && c.dataset.club) { e.preventDefault(); e.stopPropagation(); location.hash = '#/club/' + c.dataset.club; }
}, true);
const GRUPO_POS = (p) => { p = (p || '').toLowerCase(); if (p.includes('goal')) return 'Arqueros'; if (p.includes('def') || p.includes('back')) return 'Defensores'; if (p.includes('mid')) return 'Mediocampistas'; if (p.includes('off') || p.includes('forw') || p.includes('wing') || p.includes('strik')) return 'Delanteros'; return 'Otros'; };
async function vistaClub(id) {
  $('vista').innerHTML = volver('Club') + '<div class="cargando" id="cargaClub">Cargando el club…</div>';
  const aviso = setTimeout(() => { const el = $('cargaClub'); if (el) el.innerHTML = 'Cargando el club…<br><small>Está tardando un poco: la API gratis permite pocos pedidos por minuto. En unos segundos aparece.</small>'; }, 5000);
  let c; try { c = await pedir('/api/club?id=' + id); } catch (e) { clearTimeout(aviso); $('vista').innerHTML = volver('Club') + `<main>${error(e)}</main>`; return; }
  clearTimeout(aviso);
  dibujarClub(c);
  pedir('/api/club_extra?id=' + id).then(x => {
    if (location.hash !== '#/club/' + id) return;
    Object.entries(x.fotos || {}).forEach(([n, f]) => { FOTOS[n] = f; });
    if (x.plantel && x.plantel.length && !c.plantel.length) c.plantel = x.plantel;
    ['capacidad', 'ubicacion', 'fotoEstadio', 'camisetas', 'descripcion', 'apodo', 'wikipedia'].forEach(k => { if (x[k] && (!Array.isArray(x[k]) || x[k].length)) c[k] = x[k]; });
    c.fundacion = c.fundacion || x.fundacion; c.estadio = c.estadio || x.estadio; c.dt = c.dt || x.dt;
    const y = window.scrollY; dibujarClub(c); window.scrollTo(0, y);
  }).catch(() => {});
}
function dibujarClub(c) {
  const dato = (t, v) => v ? `<div><small>${t}</small><b>${esc(v)}</b></div>` : '';
  const grupos = {};
  c.plantel.forEach(j => (grupos[GRUPO_POS(j.posicion)] ||= []).push(j));
  const orden = ['Arqueros', 'Defensores', 'Mediocampistas', 'Delanteros', 'Otros'];
  $('vista').innerHTML = volver(c.corto) + `
    <section class="portada" style="${c.fotoEstadio ? `background-image:linear-gradient(to top, rgba(5,7,13,1) 10%, rgba(5,7,13,.35)), url('${esc(c.fotoEstadio)}')` : ''}">
      ${c.escudo ? `<img src="${esc(c.escudo)}" alt="" class="escudo-xl">` : ''}
      <b class="titulo" style="font-size:24px">${esc(c.nombre)}</b>
      ${c.estadio ? `<small style="color:#C7D2E0">${esc(c.estadio)}${c.ubicacion ? ' · ' + esc(c.ubicacion) : ''}</small>` : ''}
    </section>
    <main>
      <section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">Datos del club</h2><div class="datos3">
        ${dato('Fundación', c.fundacion)}${dato('Estadio', c.estadio)}${dato('Capacidad', c.capacidad ? Number(c.capacidad).toLocaleString('es-AR') + ' personas' : '')}
        ${dato('Ubicación', c.ubicacion || c.direccion)}${dato('Apodo', c.apodo)}${dato('Colores', c.colores)}${dato('Director técnico', c.dt)}</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap">${c.web ? `<a class="link" href="${esc(c.web)}" target="_blank" rel="noopener">Sitio oficial ↗</a>` : ''}${c.wikipedia ? `<a class="link" href="${esc(c.wikipedia)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ''}</div></section>
      ${c.camisetas.length ? `<section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">Camisetas</h2>
        <div class="camisetas">${c.camisetas.map(k => `<figure>${k.imagen ? `<img src="${esc(k.imagen)}" alt="Camiseta ${esc(k.tipo)}" loading="lazy">` : camisetaDibujo(k)}<figcaption>${esc(k.tipo)}${k.aprox ? '<span class="aprox">Colores del club</span>' : ''}</figcaption></figure>`).join('')}</div></section>` : ''}
      <section class="tarjeta"><div class="cab"><div class="t"><b>Próximos partidos</b><small>Los próximos ${c.proximos.length || 5}</small></div></div>
        ${c.proximos.length ? c.proximos.map(p => { const d = new Date(p.fecha + 'T12:00:00'); return `<div style="padding:8px 12px 0;font-size:11px;font-weight:700;color:var(--suave)">${DIAS[d.getDay()]} ${d.getDate()}/${d.getMonth()+1} · ${esc(p.ligaNombre)}</div>` + filaPartido(p); }).join('') : '<div class="aviso">No hay partidos programados.</div>'}</section>
      ${c.plantel.length ? `<section class="tarjeta"><div class="cab"><div class="t"><b>Plantel actual</b><small>${c.plantel.length} jugadores</small></div></div>
        ${orden.filter(g => grupos[g]).map(g => `<div style="padding:10px 12px 2px;font-size:11px;font-weight:800;letter-spacing:.6px;color:var(--suave)">${g.toUpperCase()}</div>` +
          grupos[g].map(j => `<a class="jug" style="grid-template-columns:40px 1fr auto" href="#/ficha/${encodeURIComponent(j.nombre)}">${caraHTML(j)}<span><b style="font-size:14px">${esc(j.nombre)}</b><br><small style="color:var(--suave)">${esc(j.nacionalidad)}</small></span><small style="color:var(--suave)">${edad(j.nacimiento)}</small></a>`).join('')).join('')}</section>` : ''}
      ${c.descripcion ? `<section class="tarjeta pad"><h2 style="margin:0 0 8px;font-size:15px">Historia</h2><p style="margin:0;font-size:13px;line-height:1.55;color:#C7D2E0">${esc(c.descripcion)}…</p></section>` : ''}
      <p class="nota-pie">Datos de football-data.org, Highlightly, TheSportsDB y Wikipedia.</p>
    </main>`;
  c.plantel.forEach(j => { if (j.foto) FOTOS[j.nombre] = j.foto; });
  cargarFotos($('vista'));
}
/* ---------- Camiseta dibujada como en Wikipedia: colores de fondo + el diseño encima ---------- */
const WIKI_ARCH = { la: 'Kit_left_arm', b: 'Kit_body', ra: 'Kit_right_arm', sh: 'Kit_shorts', so: 'Kit_socks' };
const WIKI_COLOR = { la: 'leftarm', b: 'body', ra: 'rightarm', sh: 'shorts', so: 'socks' };
// Medidas de cada parte en Wikipedia (si la página no las trae)
const KIT_POS = { la:{left:0,top:0,width:31,height:59}, b:{left:31,top:0,width:38,height:59}, ra:{left:69,top:0,width:31,height:59},
                  sh:{left:0,top:59,width:100,height:36}, so:{left:0,top:95,width:100,height:40} };
function camisetaWiki(k) {
  let alto = 0;
  const partes = Object.keys(KIT_POS).filter(p => k.partes[p]).map(p => {
    const x = k.partes[p], q = Object.assign({}, KIT_POS[p], x.pos || {});
    alto = Math.max(alto, q.top + q.height);
    const fondo = /^#[0-9a-f]{3,8}$|^[a-z]{3,20}$|^rgb\([\d,\s]+\)$/i.test(x.color || '') ? x.color : 'transparent';
    return `<div style="left:${q.left}px;top:${q.top}px;width:${q.width}px;height:${q.height}px;background:${fondo}"><img src="${esc(x.img)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()"></div>`;
  }).join('');
  return `<div class="kit pix" role="img" aria-label="Camiseta ${esc(k.tipo)}" style="height:${alto || 135}px">${partes}</div>`;
}
// Camiseta pixel art (16 x 16 cuadraditos), con el diseño tradicional del club
function camisetaPixel(x) {
  const W = 16, H = 16, celdas = {};
  const pon = (cx, cy, t) => { celdas[cx + ',' + cy] = t; };
  for (let cx = 2; cx <= 13; cx++) if (cx < 6 || cx > 9) pon(cx, 2, 'm');      // hombros
  pon(6, 2, 'k'); pon(9, 2, 'k');                                              // cuello
  for (let cy = 3; cy <= 5; cy++) for (let cx = 1; cx <= 14; cx++) pon(cx, cy, cx < 4 || cx > 11 ? 'm' : 'b');
  pon(7, 3, 'k'); pon(8, 3, 'k');
  for (let cx = 1; cx <= 14; cx++) pon(cx, 6, cx < 4 || cx > 11 ? 'p' : 'b');  // puños
  for (let cy = 7; cy <= 14; cy++) for (let cx = 4; cx <= 11; cx++) pon(cx, cy, 'b');
  const c1 = x.c1, c2 = x.c2, c3 = x.c3 || x.c2;
  const cuerpo = (cx, cy) => {
    const bx = cx - 4, by = cy - 3;
    switch (x.p) {
      case 'franja': return by >= 4 && by <= 6 ? c2 : c1;
      case 'banda': return Math.abs(bx - by * 7 / 11) <= 0.9 ? c2 : c1;
      case 'rayas': return (bx < 4 ? bx : 7 - bx) % 2 === 0 ? c1 : c2;  // simétrica
      case 'rayas3': return [c1, c2, c3][bx % 3];
      case 'mitad': return bx < 4 ? c1 : c2;
      case 'aros': return Math.floor(by / 2) % 2 === 0 ? c1 : c2;
      case 'centro': return (x.ancho ? bx >= 2 && bx <= 5 : bx >= 3 && bx <= 4) ? c2 : c1;
      case 'diag': return bx + by * 8 / 12 < 7 ? c1 : c2;
      case 'cuadros': return (bx + by) % 2 === 0 ? c1 : c2;
      case 'v': { const d = Math.min(bx, 7 - bx); return (by === d || by === d + 1) && by <= 4 ? c2 : c1; }
      default: return c1;
    }
  };
  const manga = (cx, cy) => x.p === 'aros' ? (Math.floor((cy - 3) / 2) % 2 === 0 ? c1 : c2) : (x.m || (x.p === 'mitad' ? (cx < 8 ? c1 : c2) : c1));
  const detalle = x.k || (x.p === 'liso' ? c2 : (x.m && x.m !== c1 ? x.m : c2));
  let r = '';
  for (let cy = 0; cy < H; cy++) for (let cx = 0; cx < W; cx++) {
    const t = celdas[cx + ',' + cy];
    let color = null;
    if (t === 'b') color = cuerpo(cx, cy);
    else if (t === 'm') color = manga(cx, cy);
    else if (t === 'k' || t === 'p') color = detalle;
    else if ([[1,0],[-1,0],[0,1],[0,-1],[1,1],[-1,-1],[1,-1],[-1,1]].some(([a, b]) => celdas[(cx + a) + ',' + (cy + b)])) color = '#111';
    if (color) r += `<rect x="${cx}" y="${cy}" width="1" height="1" fill="${color}"/>`;
  }
  // cuadrícula finita, como en papel cuadriculado
  let grilla = '';
  for (let i = 1; i < W; i++) grilla += `<line x1="${i}" y1="0" x2="${i}" y2="${H}"/><line x1="0" y1="${i}" x2="${W}" y2="${i}"/>`;
  return `<svg class="kit-pix" viewBox="0 0 ${W} ${H}" shape-rendering="crispEdges" role="img">${r}<g stroke="rgba(0,0,0,.18)" stroke-width=".05">${grilla}</g></svg>`;
}
function camisetaDibujo(k) {
  if (k.pix) return camisetaPixel(k.pix);
  if (k.partes) return camisetaWiki(k);
  const color = (v) => /^#[0-9a-f]{3,6}$|^[a-z]{3,20}$/i.test(v || '') ? v : '#888';
  if (!Object.values(k.dibujo || {}).some(Boolean)) return camisetaSVG(k);  // lisa: dibujo propio
  return `<div class="kit" role="img" aria-label="Camiseta ${esc(k.tipo)}" data-kit="${esc(JSON.stringify(k))}">${Object.keys(WIKI_ARCH).map(p => {
    const dib = (k.dibujo || {})[p] || '';
    const base = WIKI_ARCH[p] + (p === 'so' ? '_long' : '') + '.svg';
    const opciones = dib ? ['es', 'en'].map(w => `https://${w}.wikipedia.org/wiki/Special:FilePath/${WIKI_ARCH[p]}${dib}.png`) : [];
    opciones.push('https://en.wikipedia.org/wiki/Special:FilePath/' + base);
    return `<div class="${p}" style="background:${color(k.colores[WIKI_COLOR[p]])}"><img src="${esc(opciones[0])}" data-alt="${esc(opciones.slice(1).join('|'))}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="kitFalla(this)"></div>`;
  }).join('')}</div>`;
}
// Camiseta dibujada por nosotros (si Wikipedia no tiene el diseño o no carga)
function camisetaSVG(k) {
  const c = (n) => { const v = (k.colores || {})[n]; return /^#[0-9a-f]{3,6}$|^[a-z]{3,20}$/i.test(v || '') ? v : '#888'; };
  const borde = 'stroke="rgba(0,0,0,.35)" stroke-width="1.2" stroke-linejoin="round"';
  return `<svg class="kit-svg" viewBox="0 0 100 135" role="img" aria-label="Camiseta ${esc(k.tipo)}">
    <polygon points="31,8 20,6 2,24 12,40 31,30" fill="${c('leftarm')}" ${borde}/>
    <polygon points="69,8 80,6 98,24 88,40 69,30" fill="${c('rightarm')}" ${borde}/>
    <path d="M31,8 L41,3 Q50,11 59,3 L69,8 L69,62 L31,62 Z" fill="${c('body')}" ${borde}/>
    <path d="M41,3 Q50,11 59,3" fill="none" stroke="rgba(0,0,0,.35)" stroke-width="2"/>
    <polygon points="29,64 71,64 75,95 54,95 50,82 46,95 25,95" fill="${c('shorts')}" ${borde}/>
    <rect x="30" y="99" width="14" height="32" rx="3" fill="${c('socks')}" ${borde}/>
    <rect x="56" y="99" width="14" height="32" rx="3" fill="${c('socks')}" ${borde}/></svg>`;
}
function kitFalla(img) {
  const resto = (img.dataset.alt || '').split('|').filter(Boolean);
  if (!resto.length) {
    const kit = img.closest('.kit');
    if (kit && !kit.dataset.cambiado) { kit.dataset.cambiado = 1; try { kit.outerHTML = camisetaSVG(JSON.parse(kit.dataset.kit)); } catch (e) {} }
    else img.remove();
    return;
  }
  img.dataset.alt = resto.slice(1).join('|'); img.src = resto[0];
}
/* ---------- Caras de los jugadores ---------- */
const FOTOS = {};
// Sin caras: cada jugador se muestra con un círculo con sus iniciales (más rápido)
function caraHTML(j, cls = 'avatar') { return `<span class="${cls}">${iniciales(j.nombre)}</span>`; }
function cargarFotos() {}
/* ---------- Ficha extendida de un jugador: foto, datos y clubes anteriores ---------- */
async function bloqueJugadorExtra(nombre, destino) {
  const el = $(destino); if (!el) return;
  try {
    const x = await pedir('/api/jugador_extra?nombre=' + encodeURIComponent(nombre));
    if (!x.encontrado) { el.innerHTML = '<p class="nota-pie">No encontramos más información de este jugador.</p>'; return; }
    const fila = (t, v) => v ? `<div><small>${t}</small><b>${esc(v)}</b></div>` : '';
    el.innerHTML = `<section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">Más datos</h2><div class="datos3">
        ${fila('Altura', x.altura)}${fila('Número', x.numero)}${fila('Posición', x.posicion)}${fila('Club actual', x.equipo)}</div></section>
      ${x.anteriores.length ? `<section class="tarjeta"><div class="cab"><div class="t"><b>Clubes por los que pasó</b><small>Trayectoria</small></div></div>
        ${x.anteriores.map(f => `<div class="jug" style="grid-template-columns:32px 1fr auto">${f.escudo ? `<img src="${esc(f.escudo)}" alt="" style="width:28px;height:28px;object-fit:contain">` : '<span></span>'}<b style="font-size:14px">${esc(f.club)}</b><small style="color:var(--suave)">${esc(f.desde)}${f.hasta ? ' – ' + esc(f.hasta) : ''}${f.tipo === 'Loan' ? ' · préstamo' : ''}</small></div>`).join('')}</section>` : ''}
      <p class="nota-pie">Goles, partidos y asistencias de cada club anterior: no vienen en las fuentes gratuitas. Se pueden sumar con una API más completa.</p>`;
  } catch (e) { el.innerHTML = ''; }
}
async function vistaFicha(nombre) {
  nombre = decodeURIComponent(nombre || '');
  let j = null, cod = null;
  for (const [k, d] of Object.entries(cacheGol)) { const f = d.goleadores.find(x => x.nombre === nombre); if (f) { j = f; cod = k; break; } }
  if (j) { location.replace(`#/jugador/${cod}/${j.id}`); return; }
  $('vista').innerHTML = volver('Jugador', 'javascript:history.back()') + `<section style="background:var(--negro);padding:6px 16px 18px;border-bottom:1px solid var(--linea)">
      <div style="display:flex;align-items:center;gap:14px"><span class="avatar xl">${iniciales(nombre)}</span><b class="titulo" style="font-size:22px">${esc(nombre)}</b></div></section>
    <main id="extra"><div class="cargando">Buscando información…</div></main>`;
  bloqueJugadorExtra(nombre, 'extra');
}

/* ---------- Rutas ---------- */
function ruta() {
  const partes = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const [v, a, b] = partes;
  cerrarMenu();
  window.scrollTo(0, 0);
  const nav = { '': 'inicio', dia: 'inicio', calendario: 'calendario', favoritos: 'favoritos', jugadores: 'jugadores', jugador: 'jugadores', comparar: 'jugadores', ficha: 'jugadores' };
  navAbajo(nav[v || ''] || '');
  if (!v) return vistaInicio();
  if (v === 'dia') return vistaInicio(a);
  if (v === 'partido') return vistaPartido(a);
  if (v === 'liga' && LIGAS[a]) return vistaLiga(a, b);
  if (v === 'calendario') return vistaCalendario();
  if (v === 'jugadores') return vistaJugadores();
  if (v === 'jugador') return vistaJugador(a, b);
  if (v === 'comparar') return vistaComparar(a, b);
  if (v === 'favoritos') return vistaFavoritos();
  if (v === 'club') return vistaClub(a);
  if (v === 'ficha') return vistaFicha(a);
  vistaInicio();
}
armarMenu();
window.addEventListener('hashchange', ruta);
ruta();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# El servidor: responde a la página
# ---------------------------------------------------------------------------
class Manejador(BaseHTTPRequestHandler):
    def responder(self, codigo, tipo, cuerpo):
        datos = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def json(self, funcion):
        try:
            self.responder(200, "application/json", json.dumps(funcion()))
        except Exception as e:
            self.responder(502, "application/json", json.dumps({"error": str(e)}))

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        liga = q.get("liga", "")
        if url.path == "/":
            visibles = {**({k: list(v) for k, v in HL_LIGAS.items()} if HL_KEY else {}), **{k: list(v) for k, v in LIGAS.items()}}
            pagina = PAGINA.replace("__LIGAS__", json.dumps(visibles, ensure_ascii=False)).replace("__HL__", json.dumps(list(HL_LIGAS) if HL_KEY else []))
            self.responder(200, "text/html; charset=utf-8", pagina)
        elif url.path == "/logo.png":
            ruta = os.path.join(CARPETA, "tikidata_escudo.png")
            if os.path.exists(ruta):
                with open(ruta, "rb") as f:
                    self.responder(200, "image/png", f.read())
            else:
                self.responder(404, "text/plain", "Sin logo")
        elif url.path.startswith("/api/") and liga and liga not in LIGAS and not (HL_KEY and liga in HL_LIGAS):
            self.responder(400, "application/json", json.dumps({"error": "Liga no disponible"}))
        elif url.path == "/api/hoy":
            fecha = q.get("fecha") or datetime.now(ARG).strftime("%Y-%m-%d")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
                self.responder(400, "application/json", json.dumps({"error": "Fecha inválida"}))
                return
            self.json(lambda: datos_hoy(fecha))
        elif url.path == "/api/calendario":
            self.json(lambda: datos_calendario(liga))
        elif url.path == "/api/partido" and re.fullmatch(r"h?\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_partido(q["id"]))
        elif url.path == "/api/tabla":
            self.json(lambda: datos_tabla(liga))
        elif url.path == "/api/goleadores":
            self.json(lambda: datos_goleadores(liga))
        elif url.path == "/api/equipos":
            self.json(lambda: datos_equipos(liga))
        elif url.path == "/api/resumen" and re.fullmatch(r"h?\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_resumen(q["id"]))
        elif url.path == "/api/club_extra" and re.fullmatch(r"h?\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_club_extra(q["id"]))
        elif url.path == "/api/club" and re.fullmatch(r"h?\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_club(q["id"]))
        elif url.path == "/api/foto" and 0 < len(q.get("nombre", "")) <= 80:
            self.json(lambda: foto_jugador(q["nombre"]))
        elif url.path == "/api/jugador_extra" and 0 < len(q.get("nombre", "")) <= 80:
            self.json(lambda: datos_jugador_extra(q["nombre"]))
        elif url.path == "/api/proximos" and re.fullmatch(r"h?\d{1,12}", q.get("equipo", "")):
            self.json(lambda: datos_proximos(q["equipo"]))
        elif url.path == "/api/estadisticas" and re.fullmatch(r"h\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_estadisticas_hl(q["id"][1:]))
        elif url.path == "/api/hl_estado":
            self.json(hl_estado)
        elif url.path == "/api/noticias":
            prefs = re.sub(r"[^a-z,]", "", q.get("prefs", ""))[:60]
            self.json(lambda: datos_noticias(prefs))
        elif url.path == "/api/noticias_estado":
            self.json(lambda: {"portales": _estado_feeds, "fotos_buscadas": len(_fotos),
                               "fotos_encontradas": sum(1 for f, _ in _fotos.values() if f),
                               "noticias_con_foto": len(_noticias["lista"])})
        else:
            self.responder(404, "text/plain; charset=utf-8", "No encontrado")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not CLAVE:
        sys.exit("Falta la clave. En la compu: set FOOTBALL_DATA_KEY=tu_clave. En Render: cargala en Environment.")
    puerto_web = os.environ.get("PORT")
    # Prepara las noticias apenas arranca, así el primero que entra no espera
    threading.Thread(target=lambda: traer_todas(), daemon=True).start()
    if puerto_web:
        # Modo internet (Render): escucha en el puerto que le da el servicio
        servidor = ThreadingHTTPServer(("0.0.0.0", int(puerto_web)), Manejador)
        print(f"Tiki Data publicado, escuchando en el puerto {puerto_web}", flush=True)
        servidor.serve_forever()
    else:
        modo_celular = len(sys.argv) > 1 and sys.argv[1].lower() == "celular"
        servidor = ThreadingHTTPServer(("0.0.0.0" if modo_celular else "127.0.0.1", PUERTO), Manejador)
        direccion = f"http://localhost:{PUERTO}"
        print(f"\n⚽ Tiki Data funcionando en {direccion}")
        print("   Para apagarlo apretá Ctrl + C\n")
        webbrowser.open(direccion)
        try:
            servidor.serve_forever()
        except KeyboardInterrupt:
            print("\nTiki Data apagado. ¡Hasta la próxima!")
