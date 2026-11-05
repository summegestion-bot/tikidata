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
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/futbol-internacional.xml"),
    ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/portada/"),
    ("SPORT", "https://www.sport.es/es/rss/last-news/news.xml"),
    ("INFOBAE", "https://www.infobae.com/arc/outboundfeeds/rss/category/deportes/"),
    ("LA NACIÓN", "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/deportes/"),
    ("CLARÍN", "https://www.clarin.com/rss/deportes/"),
    ("OLÉ", "https://www.ole.com.ar/rss/ultimas-noticias/"),
]
# Búsquedas en Google Noticias (Argentina) para asegurar cada tema.
# Traen titulares de Olé, TyC, ESPN, etc. (sin foto).
BUSQUEDAS = [
    '"Liga Profesional" OR Boca OR River OR Racing OR Independiente OR "San Lorenzo"',
    '"Primera Nacional" OR "ascenso" fútbol',
    '"Selección Argentina" OR Scaloni',
    'Messi OR "Cristiano Ronaldo" OR Mbappé OR Haaland OR "Lamine Yamal"',
    '"Real Madrid" OR Barcelona OR "Premier League" OR clásico fútbol',
    'fichaje OR "mercado de pases" fútbol',
]
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


def datos_proximos(id_equipo):
    if str(id_equipo).startswith("h"):
        return {"partidos": proximos_hl(str(id_equipo), 3)}
    datos = api(f"/teams/{id_equipo}/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED&limit=3", ttl=900)
    return {"partidos": [partido(p) for p in datos.get("matches", [])]}


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
            lista += [p for p in hl_temporada(c) if not p["terminado"] and p["_inicio"] > ahora - 7200
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
        prox = api(f"/teams/{id_equipo}/matches?status=SCHEDULED,TIMED&limit=5", ttl=900)
        club["proximos"] = [partido(p) for p in prox.get("matches", [])][:5]
    except Exception:
        club["proximos"] = []
    return club


def datos_club_extra(id_equipo):
    """Lo que viene de TheSportsDB: estadio, foto, camisetas, historia y caras. Se carga después."""
    from concurrent.futures import ThreadPoolExecutor
    if str(id_equipo).startswith("h"):
        n = hl_equipo(str(id_equipo))["nombre"]
        t = {"shortName": n, "name": n, "squad": None}
    else:
        t = api(f"/teams/{id_equipo}", ttl=86400)
    res = {"capacidad": None, "ubicacion": "", "fundacion": None, "estadio": "", "descripcion": "",
           "fotoEstadio": "", "camisetas": [], "fotos": {}}
    extra = buscar_equipo_tsdb([t.get("shortName"), t.get("name")])
    if not extra:
        return res
    res.update(capacidad=extra.get("intStadiumCapacity"), ubicacion=extra.get("strLocation") or "",
               fundacion=extra.get("intFormedYear"), estadio=extra.get("strStadium") or "",
               descripcion=(extra.get("strDescriptionES") or extra.get("strDescriptionEN") or "")[:600],
               fotoEstadio=extra.get("strStadiumThumb") or "")
    id_t, id_v = extra.get("idTeam"), extra.get("idVenue")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_venue = ex.submit(tsdb, f"/lookupvenue.php?id={id_v}") if id_v else None
        f_kits = ex.submit(tsdb, f"/lookupequipment.php?id={id_t}") if id_t else None
        f_jug = ex.submit(tsdb, f"/lookup_all_players.php?id={id_t}") if id_t else None
        venue = (primera_lista(f_venue.result()) or [{}])[0] if f_venue else {}
        kits = primera_lista(f_kits.result()) if f_kits else []
        jugadores = primera_lista(f_jug.result()) if f_jug else []
    res["fotoEstadio"] = venue.get("strThumb") or venue.get("strFanart1") or res["fotoEstadio"] or extra.get("strFanart1") or ""
    res["capacidad"] = res["capacidad"] or venue.get("intCapacity")
    res["ubicacion"] = res["ubicacion"] or venue.get("strLocation") or ""
    kits = sorted(kits, key=lambda k: k.get("strSeason") or "", reverse=True)
    tipos = {"1st": "Titular", "2nd": "Suplente", "3rd": "Alternativa"}
    temporada = kits[0].get("strSeason") if kits else None
    for k in kits:
        if k.get("strSeason") == temporada and k.get("strEquipment") and len(res["camisetas"]) < 3:
            res["camisetas"].append({"tipo": tipos.get(k.get("strType"), k.get("strType") or ""), "imagen": k["strEquipment"]})
    if not res["camisetas"] and extra.get("strEquipment"):
        res["camisetas"].append({"tipo": "Titular", "imagen": extra["strEquipment"]})
    por_nombre = {}
    for jp in jugadores:
        f = jp.get("strCutout") or jp.get("strThumb") or ""
        if f and jp.get("strPlayer"):
            por_nombre[normalizar(jp["strPlayer"])] = f
    for j in (t.get("squad") or []):
        f = por_nombre.get(normalizar(j.get("name", "")))
        if f:
            res["fotos"][j.get("name", "")] = f
    if t.get("squad") is None:  # equipos de Highlightly: plantel desde TheSportsDB
        res["plantel"] = [{"id": jp.get("idPlayer"), "nombre": jp.get("strPlayer") or "", "posicion": jp.get("strPosition") or "",
                           "nacimiento": jp.get("dateBorn") or "", "nacionalidad": jp.get("strNationality") or "",
                           "foto": jp.get("strCutout") or jp.get("strThumb") or ""} for jp in jugadores if jp.get("strPlayer")]
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
MEZCLA = [("arg asc sel", 40), ("top pl liga imp", 30), ("est", 20), ("fich otros", 10)]
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
    except Exception:
        return []
    salida = []
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        if not titulo or not enlace:
            continue
        origen = fuente or (item.findtext("source") or "NOTICIAS").strip().upper()
        if not fuente and " - " in titulo:
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
    return salida


def traer_todas():
    if time.time() - _noticias["t"] < 900 and _noticias["lista"]:
        return _noticias["lista"]
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
    for g in grupos:
        horas = (ahora - g["orden"]) / 3600 if g["orden"] else 48
        g["puntaje"] = (max(0.0, 48 - horas) / 48) + 1.2 * (len(g["_fuentes"]) - 1) + (1.5 if g["imp"] else 0) + (0.3 if g["imagen"] else 0)
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
  for (const f of favs.slice(0, 3)) {
    try { const d = await pedir('/api/proximos?equipo=' + f.id); if (d.partidos[0]) filas.push(d.partidos[0]); } catch (e) {}
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
    ['capacidad', 'ubicacion', 'fotoEstadio', 'camisetas', 'descripcion'].forEach(k => { if (x[k] && (!Array.isArray(x[k]) || x[k].length)) c[k] = x[k]; });
    c.fundacion = c.fundacion || x.fundacion; c.estadio = c.estadio || x.estadio;
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
        ${dato('Ubicación', c.ubicacion || c.direccion)}${dato('Colores', c.colores)}${dato('Director técnico', c.dt)}</div>
        ${c.web ? `<a class="link" href="${esc(c.web)}" target="_blank" rel="noopener">Sitio oficial ↗</a>` : ''}</section>
      ${c.camisetas.length ? `<section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">Camisetas</h2>
        <div class="camisetas">${c.camisetas.map(k => `<figure><img src="${esc(k.imagen)}" alt="Camiseta ${esc(k.tipo)}" loading="lazy"><figcaption>${esc(k.tipo)}</figcaption></figure>`).join('')}</div></section>` : ''}
      <section class="tarjeta"><div class="cab"><div class="t"><b>Próximos partidos</b><small>Los próximos ${c.proximos.length || 5}</small></div></div>
        ${c.proximos.length ? c.proximos.map(p => { const d = new Date(p.fecha + 'T12:00:00'); return `<div style="padding:8px 12px 0;font-size:11px;font-weight:700;color:var(--suave)">${DIAS[d.getDay()]} ${d.getDate()}/${d.getMonth()+1} · ${esc(p.ligaNombre)}</div>` + filaPartido(p); }).join('') : '<div class="aviso">No hay partidos programados.</div>'}</section>
      ${c.plantel.length ? `<section class="tarjeta"><div class="cab"><div class="t"><b>Plantel actual</b><small>${c.plantel.length} jugadores</small></div></div>
        ${orden.filter(g => grupos[g]).map(g => `<div style="padding:10px 12px 2px;font-size:11px;font-weight:800;letter-spacing:.6px;color:var(--suave)">${g.toUpperCase()}</div>` +
          grupos[g].map(j => `<a class="jug" style="grid-template-columns:40px 1fr auto" href="#/ficha/${encodeURIComponent(j.nombre)}">${caraHTML(j)}<span><b style="font-size:14px">${esc(j.nombre)}</b><br><small style="color:var(--suave)">${esc(j.nacionalidad)}</small></span><small style="color:var(--suave)">${edad(j.nacimiento)}</small></a>`).join('')).join('')}</section>` : ''}
      ${c.descripcion ? `<section class="tarjeta pad"><h2 style="margin:0 0 8px;font-size:15px">Historia</h2><p style="margin:0;font-size:13px;line-height:1.55;color:#C7D2E0">${esc(c.descripcion)}…</p></section>` : ''}
      <p class="nota-pie">Datos de football-data.org y TheSportsDB.</p>
    </main>`;
  c.plantel.forEach(j => { if (j.foto) FOTOS[j.nombre] = j.foto; });
  cargarFotos($('vista'));
}
/* ---------- Caras de los jugadores ---------- */
const FOTOS = {};
// Versión chica de la foto (pesa mucho menos). Si no existe, usa la original.
function fotoChica(f) { return /thesportsdb\.com/.test(f) && !/\/(tiny|small|preview)$/.test(f) ? f + '/small' : f; }
function imgCara(f, cls) {
  return `<img class="${cls} con-foto" src="${esc(fotoChica(f))}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="if(!this.dataset.o){this.dataset.o=1;this.src='${esc(f)}'}">`;
}
function caraHTML(j, cls = 'avatar') {
  const f = FOTOS[j.nombre] || j.foto;
  return f ? imgCara(f, cls) : `<span class="${cls}" data-foto="${esc(j.nombre)}">${iniciales(j.nombre)}</span>`;
}
const vigia = 'IntersectionObserver' in window ? new IntersectionObserver((entradas) => {
  entradas.forEach(e => { if (e.isIntersecting) { vigia.unobserve(e.target); buscarCara(e.target.dataset.foto); } });
}, { rootMargin: '300px' }) : null;
const colaCaras = []; let buscando = 0;
function buscarCara(n) {
  if (!n || n in FOTOS || colaCaras.includes(n)) return;
  colaCaras.push(n); siguienteCara();
}
async function siguienteCara() {
  while (buscando < 3 && colaCaras.length) {
    const n = colaCaras.shift(); buscando++;
    pedir('/api/foto?nombre=' + encodeURIComponent(n)).then(r => { FOTOS[n] = r.foto || ''; }).catch(() => { FOTOS[n] = ''; }).finally(() => {
      buscando--;
      if (FOTOS[n]) document.querySelectorAll('[data-foto]').forEach(el => { if (el.dataset.foto === n) el.outerHTML = imgCara(FOTOS[n], el.className); });
      siguienteCara();
    });
  }
}
async function cargarFotos(raiz, alTerminar) {
  if (vigia) { raiz.querySelectorAll('[data-foto]').forEach(el => vigia.observe(el)); return; }
  const pendientes = [...new Set([...raiz.querySelectorAll('[data-foto]')].map(e => e.dataset.foto))].filter(n => !(n in FOTOS));
  let i = 0, cambios = 0;
  const trabajador = async () => {
    while (i < pendientes.length) {
      const n = pendientes[i++];
      try { const r = await pedir('/api/foto?nombre=' + encodeURIComponent(n)); FOTOS[n] = r.foto || ''; } catch (e) { FOTOS[n] = ''; }
      if (FOTOS[n]) {
        cambios++;
        raiz.querySelectorAll('[data-foto]').forEach(el => { if (el.dataset.foto === n) el.outerHTML = imgCara(FOTOS[n], el.className); });
      }
    }
  };
  await Promise.all([trabajador(), trabajador(), trabajador()]);
  if (cambios && alTerminar) alTerminar();
}
/* ---------- Ficha extendida de un jugador: foto, datos y clubes anteriores ---------- */
async function bloqueJugadorExtra(nombre, destino) {
  const el = $(destino); if (!el) return;
  try {
    const x = await pedir('/api/jugador_extra?nombre=' + encodeURIComponent(nombre));
    if (!x.encontrado) { el.innerHTML = '<p class="nota-pie">No encontramos más información de este jugador.</p>'; return; }
    if (x.foto) { const av = document.querySelector('.avatar.xl'); if (av) av.outerHTML = `<img class="foto-jug" src="${esc(/thesportsdb\.com/.test(x.foto) ? x.foto + '/medium' : x.foto)}" alt="Foto de ${esc(x.nombre)}" referrerpolicy="no-referrer" onerror="if(!this.dataset.o){this.dataset.o=1;this.src='${esc(x.foto)}'}">`; }
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
        else:
            self.responder(404, "text/plain; charset=utf-8", "No encontrado")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not CLAVE:
        sys.exit("Falta la clave. En la compu: set FOOTBALL_DATA_KEY=tu_clave. En Render: cargala en Environment.")
    puerto_web = os.environ.get("PORT")
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
