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

import base64
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
        raise RuntimeError("Ese dato no está disponible por ahora.")
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


# ---------------------------------------------------------------------------
# Respuestas rápidas: si ya hay algo guardado se muestra al instante y se actualiza por detrás
# ---------------------------------------------------------------------------
_rapido = {}
_rapido_en_curso = set()
_rapido_candado = threading.Lock()
_rapido_primera = {}
_ultima_visita = {"t": 0.0}


def rapido(clave, funcion, frescura):
    _ultima_visita["t"] = time.time()
    ahora = time.time()
    guardado = _rapido.get(clave)
    if guardado and ahora - guardado[0] < frescura:
        return guardado[1]
    if guardado:
        with _rapido_candado:
            if clave in _rapido_en_curso:
                return guardado[1]
            _rapido_en_curso.add(clave)

        def actualizar():
            try:
                _rapido[clave] = (time.time(), funcion())
            except Exception:
                pass
            finally:
                _rapido_en_curso.discard(clave)
        threading.Thread(target=actualizar, daemon=True).start()
        return guardado[1]  # se muestra lo último que había, sin esperar
    with _rapido_candado:
        evento = _rapido_primera.get(clave)
        primero = evento is None
        if primero:
            evento = _rapido_primera[clave] = threading.Event()
    if not primero:
        evento.wait(60)
        if clave in _rapido:
            return _rapido[clave][1]
    try:
        valor = funcion()
        _rapido[clave] = (time.time(), valor)
        return valor
    finally:
        if primero:
            evento.set()
            _rapido_primera.pop(clave, None)


def rapido_sin_esperar(clave, funcion, frescura):
    """Como rapido(), pero nunca hace esperar: si todavía no está, lo prepara por detrás y devuelve None."""
    guardado = _rapido.get(clave)
    if guardado and time.time() - guardado[0] < frescura:
        return guardado[1]
    with _rapido_candado:
        if clave in _rapido_en_curso or clave in _rapido_primera:
            return guardado[1] if guardado else None
        _rapido_en_curso.add(clave)

    def preparar():
        try:
            _rapido[clave] = (time.time(), funcion())
        except Exception:
            _rapido[clave] = (time.time() - frescura + 300, guardado[1] if guardado else {"partidos": []})  # reintenta en 5 min
        finally:
            _rapido_en_curso.discard(clave)
    threading.Thread(target=preparar, daemon=True).start()
    return guardado[1] if guardado else None


def datos_calendario_mes(anio, mes):
    """Todos los partidos de un mes de todas las ligas, en un solo pedido.
    Las ligas que todavía no están listas se avisan en 'faltan' y se preparan por detrás."""
    prefijo = f"{anio:04d}-{mes:02d}"
    ligas = [c for c in HL_LIGAS if c not in _hl_no_encontradas] if HL_KEY else []
    ligas += list(LIGAS)
    partidos, faltan = [], []
    for c in ligas:
        d = rapido_sin_esperar("cal" + c, lambda c=c: datos_calendario(c), 900)
        if d is None:
            faltan.append(c)
            continue
        partidos += [p for p in d.get("partidos", []) if str(p.get("fecha", "")).startswith(prefijo)]
    return {"anio": anio, "mes": mes, "partidos": partidos, "faltan": faltan}


def hoy_rapido(fecha):
    es_hoy = fecha == datetime.now(ARG).strftime("%Y-%m-%d")
    return rapido("hoy" + fecha, lambda: datos_hoy(fecha), 10 if es_hoy else 600)


def calendario_rapido(liga):
    return rapido("cal" + liga, lambda: datos_calendario(liga), 900)


def precalentar():
    """Al arrancar el servidor deja todo listo antes de que entre alguien:
    los partidos de hoy, el calendario de todas las ligas y los goleadores."""
    time.sleep(3)
    hoy = datetime.now(ARG).strftime("%Y-%m-%d")
    try:
        hoy_rapido(hoy)
    except Exception:
        pass
    ligas = list(HL_LIGAS if HL_KEY else []) + list(LIGAS)
    for c in ligas:
        try:
            calendario_rapido(c)
        except Exception:
            pass
        if c in LIGAS:
            time.sleep(12)  # football-data deja pocos pedidos por minuto: se dejan libres para los que están mirando
    # mientras haya gente mirando, los partidos de hoy se mantienen frescos por detrás
    while True:
        time.sleep(20)
        if time.time() - _ultima_visita["t"] < 30 * 60:
            try:
                datos = datos_hoy(datetime.now(ARG).strftime("%Y-%m-%d"))
                _rapido["hoy" + datetime.now(ARG).strftime("%Y-%m-%d")] = (time.time(), datos)
            except Exception:
                pass


def datos_hoy(fecha):
    grupos_hl = []
    if HL_KEY:
        def uno(cod):
            try:
                return cod, sorted(hl_hoy(cod, fecha), key=lambda p: p["hora"])
            except Exception:
                viejo = _memoria.get("hlhoy" + cod + fecha)  # si falla, se muestra lo último que había
                return cod, sorted(viejo[1], key=lambda p: p["hora"]) if viejo else []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:  # de a pocos, para no saturar a Highlightly
            resultados = list(ex.map(uno, [c for c in HL_LIGAS if c not in _hl_no_encontradas]))
        for cod, ps in resultados:
            if ps:
                grupos_hl.append({"liga": cod, "nombre": HL_LIGAS[cod][0], "region": HL_LIGAS[cod][1],
                                  "emblema": ps[0]["ligaEmblema"], "partidos": [{k: v for k, v in p.items() if k != "_inicio"} for p in ps]})
    res = datos_hoy_fd(fecha)
    res["grupos"] = sorted(grupos_hl + res["grupos"], key=lambda g: ORDEN_LIGAS.index(g["liga"]) if g["liga"] in ORDEN_LIGAS else 99)
    return res


# Orden de importancia (igual que el menú): primero lo argentino y las copas, después Europa, las 5 grandes, el ascenso y el resto
ORDEN_LIGAS = ["LPF", "LIB", "SUD", "CL", "PL", "PD", "UEL", "SA", "BL1", "FL1", "CAR", "UECL", "BSA", "PNA", "WC", "CAM", "EURO",
               "PPL", "DED", "ELC", "MLS", "LMX", "URU", "CHI", "COL", "PAR", "PER", "ECU", "BOL", "VEN", "SAU",
               "FAC", "CDR", "CIT", "DFB", "CDF", "TDC", "SCA", "FEDA", "PRC", "PRD"]


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


def colores_equipo(eq, liga):
    try:
        k = camisetas_pixel([eq["nombre"]], liga in ("LPF", "CAR", "PNA", "FEDA", "PRC", "PRD", "TDC", "SCA"))
        pix = k[0]["pix"] if k else {}
        return {"c1": pix.get("c1") or "#2F6BDB", "c2": pix.get("c2") or "#FFFFFF"}
    except Exception:
        return {"c1": "#2F6BDB", "c2": "#FFFFFF"}


def datos_partido(id_partido):
    r = _datos_partido(id_partido)
    r["colores"] = {"local": colores_equipo(r["local"], r.get("liga")), "visitante": colores_equipo(r["visitante"], r.get("liga"))}
    if r["colores"]["local"]["c1"].lower() == r["colores"]["visitante"]["c1"].lower():
        r["colores"]["visitante"] = {"c1": r["colores"]["visitante"]["c2"], "c2": r["colores"]["visitante"]["c1"]}
    return r


def _datos_partido(id_partido):
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
        previos = [partido(x) for x in h.get("matches", []) if x.get("id") != res["id"]]
        gana_l = gana_v = empates = 0
        for x in previos:
            if x["gl"] is None or x["gv"] is None or not x["terminado"]:
                continue
            if x["gl"] == x["gv"]:
                empates += 1
                continue
            ganador = x["local"]["id"] if x["gl"] > x["gv"] else x["visitante"]["id"]
            if ganador == res["local"]["id"]:
                gana_l += 1
            elif ganador == res["visitante"]["id"]:
                gana_v += 1
        res["historial"] = {"total": len(previos), "local": gana_l, "empates": empates, "visitante": gana_v, "partidos": previos}
    except Exception:
        res["historial"] = None
    return sumar_detalle_hl(res, p)


def datos_tabla(liga):
    res = _datos_tabla(liga)
    try:
        completar_tabla(res, liga)
    except Exception:
        pass
    return res


# Zonas de cada tabla: (desde, hasta, tipo, texto). N = cantidad de equipos del grupo.
# Tipos: cl/lib (copa principal), uel/sud (segunda copa), uecl (tercera), asc (ascenso), playoff, reducido, promo, desc (descenso), elim
def _zonas_europa(cl, uel=1, uecl=1, promo=0, desc=3):
    def z(N):
        r = [(1, cl, "cl", "Champions League")]
        if uel:
            r.append((cl + 1, cl + uel, "uel", "Europa League"))
        if uecl:
            r.append((cl + uel + 1, cl + uel + uecl, "uecl", "Conference League"))
        if promo:
            r.append((N - desc - promo + 1, N - desc, "promo", "Promoción"))
        r.append((N - desc + 1, N, "desc", "Descenso"))
        return r
    return z


ZONAS = {
    "PL": _zonas_europa(4, 1, 0), "PD": _zonas_europa(4), "SA": _zonas_europa(4), "BL1": _zonas_europa(4, promo=1, desc=2),
    "FL1": _zonas_europa(4, promo=1, desc=2), "PPL": _zonas_europa(2, 1, 1, promo=1, desc=2), "DED": _zonas_europa(2, 1, 1, promo=2, desc=1),
    "ELC": lambda N: [(1, 2, "asc", "Ascenso a la Premier"), (3, 6, "reducido", "Reducido por el ascenso"), (N - 2, N, "desc", "Descenso")],
    "BSA": lambda N: [(1, 6, "lib", "Copa Libertadores"), (7, 12, "sud", "Copa Sudamericana"), (N - 3, N, "desc", "Descenso")],
    "SAU": lambda N: [(N - 2, N, "desc", "Descenso")],
    "COL": lambda N: [(1, 8, "playoff", "Cuadrangulares")],
    "LMX": lambda N: [(1, 6, "playoff", "Liguilla"), (7, 10, "reducido", "Play-in")],
    "MLS": lambda N: [(1, 7, "playoff", "Playoffs"), (8, 9, "reducido", "Wild card")],
    "CL": lambda N: [(1, 8, "asc", "Octavos de final"), (9, 24, "playoff", "Playoff de ida y vuelta"), (25, N, "elim", "Eliminado")],
    "UEL": lambda N: [(1, 8, "asc", "Octavos de final"), (9, 24, "playoff", "Playoff de ida y vuelta"), (25, N, "elim", "Eliminado")],
    "UECL": lambda N: [(1, 8, "asc", "Octavos de final"), (9, 24, "playoff", "Playoff de ida y vuelta"), (25, N, "elim", "Eliminado")],
    "LIB": lambda N: [(1, 2, "asc", "Octavos de final"), (3, 3, "sud", "Pasa a la Sudamericana")],
    "SUD": lambda N: [(1, 1, "asc", "Octavos de final"), (2, 2, "playoff", "Playoff")],
    "PNA": lambda N: [(1, 1, "asc", "Final por el ascenso"), (2, 8, "reducido", "Reducido")],
}


def zonas_de(liga, nombre_grupo, N):
    n = normalizar(nombre_grupo)
    if liga == "LPF":
        if any(x in n for x in (" anual ", " annual ", " overall ", " general ", " acumulada ")):
            return [(1, 1, "lib", "Copa Libertadores"), (N, N, "desc", "Descenso por tabla anual")]
        if " promedio" in n:
            return [(N, N, "desc", "Descenso por promedios")]
        return [(1, 8, "playoff", "Playoffs")]
    f = ZONAS.get(liga)
    if not f or N < 4:
        return []
    return [z for z in f(N) if 1 <= z[0] <= z[1] <= N]


ELIMINATORIA = re.compile(r"octavos|cuartos|semi|final|round of|1/8|1/4|1/2|quarter|play-?off|knockout|repechaje", re.I)


def tabla_anual(partidos):
    """Tabla anual de la Liga Profesional: suma Apertura y Clausura (sin los playoffs)."""
    t = {}
    for p in partidos:
        if not p.get("terminado") or p.get("gl") is None or ELIMINATORIA.search(p.get("fase") or ""):
            continue
        for yo, gf, gc in ((p["local"], p["gl"], p["gv"]), (p["visitante"], p["gv"], p["gl"])):
            if yo.get("id") is None:
                continue
            f = t.setdefault(yo["id"], {"equipo": yo, "pj": 0, "g": 0, "e": 0, "p": 0, "gf": 0, "gc": 0})
            f["pj"] += 1
            f["gf"] += gf
            f["gc"] += gc
            f["g" if gf > gc else "e" if gf == gc else "p"] += 1
    filas = []
    for f in t.values():
        filas.append({"equipo": f["equipo"], "pj": f["pj"], "g": f["g"], "e": f["e"], "p": f["p"], "dg": f["gf"] - f["gc"],
                      "pts": f["g"] * 3 + f["e"], "forma": "", "_gf": f["gf"]})
    filas.sort(key=lambda f: (-f["pts"], -f["dg"], -f["_gf"], f["equipo"]["nombre"]))
    for i, f in enumerate(filas):
        f["pos"] = i + 1
        f.pop("_gf")
    return filas


def completar_tabla(res, liga):
    """Agrega a cada equipo su zona (copas, playoffs, descenso) y su próximo partido."""
    proximos = {}
    try:
        cal = calendario_rapido(liga)
        if liga == "LPF" and not any(" anual " in normalizar(g.get("nombre")) for g in res.get("grupos", [])):
            filas = tabla_anual(cal.get("partidos", []))
            if len(filas) >= 20:
                res["grupos"].insert(min(2, len(res["grupos"])), {"nombre": "Tabla anual", "filas": filas})
        hoy = datetime.now(ARG).strftime("%Y-%m-%d")
        for p in sorted(cal.get("partidos", []), key=lambda p: (p["fecha"], p.get("hora") or "")):
            if p.get("terminado") or p.get("vivo") or p["fecha"] < hoy or p.get("estado") in ("POSTPONED", "CANCELLED", "SUSPENDED"):
                continue
            for yo, rival, local in ((p["local"], p["visitante"], True), (p["visitante"], p["local"], False)):
                if yo.get("id") is not None and yo["id"] not in proximos:
                    proximos[yo["id"]] = {"rival": rival, "local": local, "fecha": p["fecha"], "hora": p.get("hora") or "", "id": p["id"]}
    except Exception:
        pass
    for g in res.get("grupos", []):
        N = len(g["filas"])
        zonas = zonas_de(liga, g.get("nombre") or "", N)
        g["zonas"] = [{"desde": a, "hasta": b, "tipo": t, "texto": x} for a, b, t, x in zonas]
        for f in g["filas"]:
            pos = f.get("pos") or 0
            f["zona"] = next((t for a, b, t, x in zonas if a <= pos <= b), "")
            f["prox"] = proximos.get(f["equipo"].get("id"))


def _datos_tabla(liga):
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


GOLEADORES_HL = ("LPF", "LIB", "SUD", "CAR", "PNA")
GOL_POR_VUELTA = 120   # partidos nuevos que se piden, como mucho, cada vez que se recalculan los goleadores de un torneo   # torneos donde se cuentan los goles partido por partido
_goleadores = {}
_goleadores_candado = threading.Lock()


def _torneo_actual(partidos):
    """Si las fechas dicen Apertura/Clausura, se cuentan solo las del torneo que se está jugando."""
    con_nombre = [p for p in partidos if re.search(r"apertura|clausura", (p.get("fase") or "").lower())]
    if not con_nombre:
        return partidos, "Temporada " + str(datetime.now(ARG).year)
    ultimo = max(con_nombre, key=lambda p: p["_inicio"])
    torneo = "clausura" if "clausura" in ultimo["fase"].lower() else "apertura"
    return [p for p in partidos if torneo in (p.get("fase") or "").lower()], torneo.title() + " " + str(datetime.now(ARG).year)


def calcular_goleadores_hl(cod):
    """Recorre los partidos terminados y suma los goles de cada jugador (cada partido se pide una sola vez)."""
    jugados = [p for p in hl_temporada(cod) if p["terminado"]]
    jugados, titulo = _torneo_actual(jugados)
    tabla = {}

    # Cada partido se pide una sola vez y queda guardado. Los que faltan se piden de a poco
    # (como mucho GOL_POR_VUELTA por vuelta) y solo si sobra cupo, así no se gastan los pedidos del día.
    restantes = _hl["restantes"] if _hl["restantes"] is not None else HL_LIMITE - _hl["usados"]
    hay_cupo = restantes > (_hl["limite"] or HL_LIMITE) * 0.4
    nuevos = {"n": 0}

    def detalle(p):
        ruta = f"/matches/{p['id'][1:]}"
        if ("hl" + ruta + "{}") not in _memoria:
            if not hay_cupo or nuevos["n"] >= GOL_POR_VUELTA:
                return p, None
            nuevos["n"] += 1
        try:
            return p, hl(ruta, ttl=60 * 86400)
        except Exception:
            return p, None
    jugados = sorted(jugados, key=lambda p: p.get("fecha") or "", reverse=True)  # primero los más nuevos
    detalles = [detalle(p) for p in jugados]
    for p, d in detalles:
        if d is None:
            continue
        m = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
        equipos = {str((m.get(k) or {}).get("id")): _equipo_hl(m.get(k)) for k in ("homeTeam", "awayTeam")}
        def fila(nombre, eq):
            return tabla.setdefault((nombre, eq["id"]), {"nombre": nombre, "equipo": eq, "goles": 0, "asistencias": 0, "penales": 0,
                                                         "amarillas": 0, "rojas": 0, "partidos": set()})
        for e in m.get("events") or []:
            tipo = e.get("type") or ""
            jug = e.get("player")
            jug = ((jug.get("name") if isinstance(jug, dict) else jug) or "").strip()
            if _desconocido(jug):
                jug = ""
            eq = equipos.get(str((e.get("team") or {}).get("id"))) or {"id": None, "nombre": "", "escudo": "", "sigla": ""}
            if jug and tipo in ("Goal", "Penalty"):
                f = fila(jug, eq)
                f["goles"] += 1
                f["penales"] += tipo == "Penalty"
                f["partidos"].add(p["id"])
            if jug and tipo == "Yellow Card":
                fila(jug, eq)["amarillas"] += 1
            if jug and re.search(r"red card|second yellow|yellow.?red", tipo, re.I):
                fila(jug, eq)["rojas"] += 1
            asis = (e.get("assist") or "").strip()
            if asis and not _desconocido(asis) and tipo == "Goal":
                fila(asis, eq)["asistencias"] += 1
    todos = list(tabla.values())
    for i, f in enumerate(todos):
        f["id"] = "g" + re.sub(r"[^a-z0-9]", "", normalizar(f["nombre"]))[:20] + str(i)
        f["partidos"] = len(f["partidos"])
        f.update(nacionalidad="", nacimiento="", posicion="")

    def top(campo, desempate):
        return [f for f in sorted(todos, key=lambda f: (-f[campo], -f[desempate], f["nombre"])) if f[campo] > 0][:30]
    return {"lista": top("goles", "asistencias"), "asistidores": top("asistencias", "goles"),
            "amarillas": top("amarillas", "rojas"), "rojas": top("rojas", "amarillas"), "titulo": titulo, "hora": time.time(),
            "faltan": sum(1 for p, d in detalles if d is None)}


def _actualizar_goleadores(cod):
    with _goleadores_candado:
        try:
            _goleadores[cod] = calcular_goleadores_hl(cod)
        except Exception as e:
            _goleadores.setdefault(cod, {"lista": [], "titulo": "", "hora": time.time(), "error": str(e)})


def goleadores_al_arrancar():
    """Busca los torneos y cuenta los goles apenas arranca el servidor (y los goles cada 3 horas), sin que nadie espere."""
    time.sleep(90)  # primero se preparan los partidos de hoy y el calendario
    for c in HL_LIGAS:
        try:
            hl_liga(c)
        except Exception:
            pass
    # Al arrancar NO se cuentan los goles de todos los torneos (eso gastaba cientos de pedidos en cada reinicio):
    # se cuentan recién cuando alguien abre la tabla de goleadores, y después se actualizan cada 2 horas.
    while True:
        time.sleep(2 * 3600)
        for c in [x for x in GOLEADORES_HL if x in _goleadores]:
            _actualizar_goleadores(c)
            time.sleep(60)


def datos_goleadores(liga):
    if liga in GOLEADORES_HL and HL_KEY:
        g = _goleadores.get(liga)
        # si alguien la mira y todavía faltan partidos por contar, se suma otra tanda (cada 10 minutos como mucho)
        if (not g or (g.get("faltan") and time.time() - g.get("hora", 0) > 600)) and not _goleadores_candado.locked():
            threading.Thread(target=_actualizar_goleadores, args=(liga,), daemon=True).start()
        if not g:
            return {"liga": liga, "nombre": HL_LIGAS[liga][0], "goleadores": [], "calculando": True}
        return {"liga": liga, "nombre": HL_LIGAS[liga][0] + (" · " + g["titulo"] if g.get("titulo") else ""), "goleadores": g["lista"],
                "asistidores": g.get("asistidores", []), "amarillas": g.get("amarillas", []), "rojas": g.get("rojas", [])}
    if liga in HL_LIGAS:
        return {"liga": liga, "nombre": HL_LIGAS[liga][0], "goleadores": []}
    datos = api(f"/competitions/{liga}/scorers?limit=60", ttl=600)
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
    asis = sorted([x for x in lista if x["asistencias"]], key=lambda x: (-x["asistencias"], -x["goles"]))
    return {"liga": liga, "nombre": LIGAS[liga][0], "goleadores": lista, "asistidores": asis, "amarillas": [], "rojas": []}


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
# + goles, formaciones y minuto a minuto de los partidos de Europa.
# Plan PRO = unos 7.500 pedidos por día (se puede cambiar con HL_LIMITE). "Uso inteligente":
#   - fuera del horario de partidos no se consulta nada (se usa lo guardado)
#   - durante los partidos se reparte lo que queda del día: 75% para la Liga Profesional
#   - solo se consulta cuando alguien está mirando la página
# Clave en la variable HIGHLIGHTLY_KEY. Opcional: HL_IDS="LPF=123,CAR=456,LIB=789,SUD=321"
# ---------------------------------------------------------------------------
HL_KEY = os.environ.get("HIGHLIGHTLY_KEY", "").strip()
HL_LIMITE = int(os.environ.get("HL_LIMITE", "7500") or 7500)   # pedidos por día del plan
HL_RESERVA = max(15, HL_LIMITE // 4)  # se guardan para fichas, formaciones, planteles y Europa
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
_EXCLUIR = (" reserva", " reserve", " femen", " women", " sub ", " u20", " u17", " u19", " u21", " u23", " youth", " juvenil", " futsal",
            " qualif", " clasif", " beach", " playoff relegation", " segunda ", " second ", " amateur")

# Ligas nuevas del plan PRO: código -> (nombre, región, búsquedas, palabras que tiene que tener el nombre)
NUEVAS_HL = {
    "PNA": ("Primera Nacional", "Argentina", [{"countryCode": "AR"}], [" primera nacional ", " primera b nacional "]),
    "FEDA": ("Federal A", "Argentina", [{"countryCode": "AR"}], [" torneo federal a ", " federal a "]),
    "PRC": ("Primera C", "Argentina", [{"countryCode": "AR"}], [" primera c "]),
    "PRD": ("Primera D", "Argentina", [{"countryCode": "AR"}], [" primera d "]),
    "TDC": ("Trofeo de Campeones", "Argentina", [{"countryCode": "AR"}], [" trofeo de campeones "]),
    "SCA": ("Supercopa Argentina", "Argentina", [{"countryCode": "AR"}], [" super copa ", " supercopa argentina "]),
    "URU": ("Liga uruguaya", "Uruguay", [{"countryCode": "UY"}, {"countryName": "Uruguay"}], [" primera division ", " liga auf ", " campeonato uruguayo "]),
    "CHI": ("Liga chilena", "Chile", [{"countryCode": "CL"}, {"countryName": "Chile"}], [" primera division ", " liga de primera "]),
    "COL": ("Liga colombiana", "Colombia", [{"countryCode": "CO"}, {"countryName": "Colombia"}], [" primera a ", " liga betplay ", " liga dimayor "]),
    "PAR": ("Liga paraguaya", "Paraguay", [{"countryCode": "PY"}, {"countryName": "Paraguay"}], [" division profesional ", " primera division "]),
    "PER": ("Liga peruana", "Perú", [{"countryCode": "PE"}, {"countryName": "Peru"}], [" liga 1 ", " primera division "]),
    "ECU": ("Liga ecuatoriana", "Ecuador", [{"countryCode": "EC"}, {"countryName": "Ecuador"}], [" liga pro ", " serie a "]),
    "BOL": ("Liga boliviana", "Bolivia", [{"countryCode": "BO"}, {"countryName": "Bolivia"}], [" division profesional ", " primera division "]),
    "VEN": ("Liga venezolana", "Venezuela", [{"countryCode": "VE"}, {"countryName": "Venezuela"}], [" liga futve ", " primera division "]),
    "MLS": ("MLS", "Estados Unidos", [{"leagueName": "Major League Soccer"}, {"leagueName": "MLS"}, {"countryCode": "US"}], [" major league soccer ", " mls "]),
    "LMX": ("Liga MX", "México", [{"leagueName": "Liga MX"}, {"countryCode": "MX"}], [" liga mx "]),
    "SAU": ("Liga saudí", "Arabia Saudita", [{"countryCode": "SA"}, {"countryName": "Saudi Arabia"}], [" pro league ", " saudi professional league ", " saudi league "]),
    "UEL": ("Europa League", "Europa", [{"leagueName": "UEFA Europa League"}, {"leagueName": "Europa League"}], [" europa league "]),
    "UECL": ("Conference League", "Europa", [{"leagueName": "UEFA Conference League"}, {"leagueName": "Conference League"}, {"leagueName": "UEFA Europa Conference League"}], [" conference league "]),
    "FAC": ("FA Cup", "Inglaterra", [{"leagueName": "FA Cup"}], [" fa cup "]),
    "CDR": ("Copa del Rey", "España", [{"leagueName": "Copa del Rey"}], [" copa del rey "]),
    "CIT": ("Copa Italia", "Italia", [{"leagueName": "Coppa Italia"}], [" coppa italia "]),
    "DFB": ("Copa de Alemania", "Alemania", [{"leagueName": "DFB Pokal"}, {"leagueName": "DFB-Pokal"}], [" dfb pokal "]),
    "CDF": ("Copa de Francia", "Francia", [{"leagueName": "Coupe de France"}], [" coupe de france "]),
    "CAM": ("Copa América", "Selecciones", [{"leagueName": "Copa America"}, {"leagueName": "CONMEBOL Copa America"}], [" copa america "]),
    "EURO": ("Eurocopa", "Selecciones", [{"leagueName": "UEFA Euro"}, {"leagueName": "Euro Championship"}, {"leagueName": "European Championship"}], [" euro championship ", " uefa euro ", " european championship ", " euro 20"]),
}
for _c, (_n, _r, _b, _p) in NUEVAS_HL.items():
    HL_LIGAS[_c] = (_n, _r)
    HL_BUSQUEDA[_c] = _b
_hl_no_encontradas = set()
_emblemas_hl = {}
_emblemas = {"hora": 0, "datos": {}}


# Ligas donde el logo de Highlightly no es el oficial (o no viene): se busca en TheSportsDB.
# código -> (países donde buscar, grupos de palabras: alcanza con que el nombre tenga TODAS las de un grupo)
LOGO_TSDB = {
    "LPF": (["Argentina"], [["primera division"], ["liga profesional"], ["superliga"]]),
    "PNA": (["Argentina"], [["primera nacional"], ["nacional b"]]),
    "URU": (["Uruguay"], [["primera division"], ["primera"]]),
    "VEN": (["Venezuela"], [["primera division"], ["futve"], ["primera"]]),
    "CDF": (["France"], [["coupe de france"]]),
    "SAU": (["Saudi-Arabia", "Saudi Arabia"], [["pro league"], ["professional league"], ["saudi", "league"]]),
    "UECL": (["Europe", "World"], [["conference league"]]),
    "EURO": (["Europe", "World"], [["european championship"], ["uefa euro"]]),
}
_NO_LOGO = (" women", " femen", " reserve", " youth", " u19", " u20", " u21", " u23", " segunda", " b metro", " cup winners")


def _logo_tsdb(cod):
    from urllib.parse import quote
    paises, grupos = LOGO_TSDB[cod]

    def sirve(lg):
        n = normalizar((lg.get("strLeague") or "") + " " + (lg.get("strLeagueAlternate") or ""))
        if any(x in n for x in _NO_LOGO) or (lg.get("strGender") or "Male").lower() == "female":
            return False
        if cod in ("LPF", "URU", "VEN") and (" nacional " in n or " primera b " in n):
            return False
        return any(all(" " + w in n for w in g) for g in grupos)
    for g in grupos:  # el primer grupo es el más preciso: se prueba en orden
        for pais in paises:
            for lg in primera_lista(tsdb(f"/search_all_leagues.php?c={quote(pais)}&s=Soccer", ttl=30 * 86400)):
                if sirve(lg) and all(" " + w in normalizar((lg.get("strLeague") or "") + " " + (lg.get("strLeagueAlternate") or "")) for w in g):
                    if lg.get("strBadge"):
                        return lg["strBadge"]
    # plan B: la lista completa de ligas y después la ficha de la elegida
    for lg in primera_lista(tsdb("/all_leagues.php", ttl=30 * 86400)):
        if (lg.get("strSport") or "Soccer") == "Soccer" and sirve(lg) and lg.get("idLeague"):
            f = (primera_lista(tsdb(f"/lookupleague.php?id={lg['idLeague']}", ttl=30 * 86400)) or [{}])[0]
            if f.get("strBadge"):
                return f["strBadge"]
    return ""


def datos_emblemas():
    """Imagen (logo) de cada liga para el menú. Se arma una vez por día."""
    if time.time() - _emblemas["hora"] < 86400 and _emblemas["datos"]:
        return _emblemas["datos"]
    res = {}
    try:
        for c in api("/competitions", ttl=7 * 86400).get("competitions", []):
            if c.get("code") in LIGAS and c.get("emblem"):
                res[c["code"]] = c["emblem"]
    except Exception:
        pass
    if HL_KEY:
        def logo(cod):
            if cod in _emblemas_hl:
                return cod, _emblemas_hl[cod]
            try:
                i, _ = hl_liga(cod)
                d = hl(f"/leagues/{i}", ttl=30 * 86400)
                d = d[0] if isinstance(d, list) and d else d
                return cod, ((d or {}).get("logo") or "") if isinstance(d, dict) else ""
            except Exception:
                return cod, ""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            for cod, url in ex.map(logo, [c for c in HL_LIGAS if c not in _hl_no_encontradas]):
                if url:
                    res[cod] = url
    for cod in LOGO_TSDB:
        if cod in LIGAS or (HL_KEY and cod in HL_LIGAS and cod not in _hl_no_encontradas):
            try:
                url = _logo_tsdb(cod)
            except Exception:
                url = ""
            if url:
                res[cod] = url  # el oficial de TheSportsDB reemplaza al de Highlightly
    _emblemas.update(hora=time.time() if res else 0, datos=res)
    return res


def _elige(cod, nombre_norm):
    if cod == "SCA" and " international " in nombre_norm:
        return False
    if any(x in nombre_norm for x in _EXCLUIR):
        return False
    if cod in NUEVAS_HL:
        if cod == "UEL" and " conference " in nombre_norm:
            return False
        if cod in ("COL", "PER", "BOL", "PAR", "VEN", "CHI", "URU") and (" b " in nombre_norm or " ascenso " in nombre_norm):
            return False
        return any(p in nombre_norm for p in NUEVAS_HL[cod][3])
    return {"LPF": (" liga profesional " in nombre_norm and " copa " not in nombre_norm) or nombre_norm.strip() == "primera division",
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
        pausa = _hl.get("pausa_hasta", 0) - ahora
        # sin cupo: cada 5 minutos se prueba igual una vez (el cupo puede renovarse a otra hora)
        sin_cupo = _hl["restantes"] is not None and _hl["restantes"] <= 1 and ahora - _hl.get("ultimo_intento", 0) < 300
        if sin_cupo or pausa > 3:
            if viejo:
                return viejo[1]
            raise RuntimeError("Se terminaron los pedidos de hoy. Mañana se actualiza solo." if sin_cupo else "Highlightly pidió esperar unos segundos.")
        _hl["ultimo_intento"] = ahora
    if pausa > 0:
        time.sleep(pausa)
    try:
        r = requests.get(HL_BASE + ruta, params=params, headers={"x-rapidapi-key": HL_KEY}, timeout=15)
        if r.status_code == 429 and str(r.headers.get("x-ratelimit-requests-remaining", "")).strip() not in ("", "0", "1"):
            # demasiados pedidos juntos (no es que se terminó el cupo): se espera un poco y se reintenta
            _hl["pausa_hasta"] = time.time() + 2
            time.sleep(2)
            r = requests.get(HL_BASE + ruta, params=params, headers={"x-rapidapi-key": HL_KEY}, timeout=15)
    except Exception as e:
        _hl["error"] = str(e)[:120]
        if viejo:
            return viejo[1]
        raise RuntimeError("Highlightly no responde en este momento.")
    with _hl_candado:
        _hl["usados"] += 1
        try:
            _hl["restantes"] = int(r.headers.get("x-ratelimit-requests-remaining"))
            _hl["limite"] = int(r.headers.get("x-ratelimit-requests-limit"))
        except (TypeError, ValueError):
            pass
    if r.status_code != 200:
        _hl["error"] = f"{r.status_code} {r.text[:120]}"
        if r.status_code == 429:
            try:
                quedan = int(r.headers.get("x-ratelimit-requests-remaining"))
            except (TypeError, ValueError):
                quedan = None
            if (quedan is not None and quedan <= 1) or "daily" in r.text.lower():
                _hl["restantes"] = 0  # ahora sí: se terminó el cupo del día
            else:
                _hl["pausa_hasta"] = time.time() + 5  # solo fueron muchos pedidos juntos
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


# Números de cada torneo en Highlightly (ya encontrados): así no hay que buscarlos cada vez que arranca el servidor
IDS_CONOCIDOS = {"LPF": (109712, 2026), "CAR": (111414, 2026), "LIB": (11847, 2026), "SUD": (10145, 2026), "PNA": (110563, 2026),
                 "URU": (228852, 2026), "CHI": (226299, 2026), "COL": (204173, 2026), "PAR": (213534, 2026), "PER": (239915, 2026),
                 "ECU": (206726, 2026), "BOL": (293528, 2026), "VEN": (255233, 2026), "MLS": (216087, 2026), "LMX": (223746, 2026),
                 "SAU": (262041, 2026), "UEL": (3337, 2026), "UECL": (722432, 2026), "FAC": (39079, 2026), "CDR": (122477, 2026),
                 "CIT": (117371, 2026), "DFB": (69715, 2026), "CDF": (56950, 2026), "CAM": (8443, 2024), "EURO": (4188, 2024)}


def hl_liga(cod):
    """(id, temporada) de cada torneo. Se busca una vez por mes, o se fija con HL_IDS."""
    fijos = dict(x.split("=", 1) for x in os.environ.get("HL_IDS", "").replace(" ", "").split(",") if "=" in x)
    if not fijos.get(cod) and cod in IDS_CONOCIDOS:
        guardado = _memoria.get("hlliga" + cod)
        if guardado:
            return guardado[1]
        # la temporada nueva se confirma de fondo una vez por día; mientras tanto se usa la conocida
        return IDS_CONOCIDOS[cod]
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
                    if lg.get("logo"):
                        _emblemas_hl[cod] = lg["logo"]
                    temporadas = [x.get("season") for x in (lg.get("seasons") or []) if isinstance(x, dict) and x.get("season")]
                    return lg.get("id"), (max(temporadas) if temporadas else datetime.now(ARG).year)
            total = ((datos or {}).get("pagination") or {}).get("totalCount") if isinstance(datos, dict) else None
            offset += 100
            if len(ligas) < 100 or (total is not None and offset >= total):
                break
    if fijos.get(cod):
        return int(fijos[cod]), datetime.now(ARG).year
    _hl.setdefault("candidatos", {})[cod] = sorted(set(vistos))[:40]
    _hl_no_encontradas.add(cod)
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
PRIORIDAD = {"boca": 1.0, "river": 1.0, "LPF": 1.0, "otros": 1.5}
MINIMO = {"boca": 1, "river": 1, "LPF": 1, "otros": 2}       # minutos mínimos entre consultas (plan PRO)
EQUIPOS_PRIORIDAD = {"boca": " boca juniors ", "river": " river plate "}


_ids_prio = {"t": 0, "ids": {}}
_intervalos = {"t": 0, "v": None}


def ids_prioridad():
    """Números de Boca y River (se recalculan cada 10 minutos: recorrer todo lo guardado es pesado)."""
    if time.time() - _ids_prio["t"] < 600 and _ids_prio["ids"]:
        return _ids_prio["ids"]
    ids = _ids_prioridad_calcular()
    _ids_prio.update(t=time.time(), ids=ids)
    return ids


def _ids_prioridad_calcular():
    """Busca los números de Boca y River en lo guardado de la Liga Profesional
    (así no se confunde con River Plate de Uruguay)."""
    ids = {}
    try:
        lpf_id = hl_liga("LPF")[0]
    except Exception:
        lpf_id = IDS_CONOCIDOS["LPF"][0]

    def mirar(nombre, ident):
        for clave, buscado in EQUIPOS_PRIORIDAD.items():
            if normalizar(nombre) == buscado and ident:
                ids[ident] = clave
    for k, (t, d) in list(_memoria.items()):
        if k.startswith("hlhoyLPF"):
            for p in d:
                mirar(p["local"]["nombre"], p["local"]["id"])
                mirar(p["visitante"]["nombre"], p["visitante"]["id"])
        elif (k.startswith("hl/matches{") or k.startswith("hl/standings")) and f'"leagueId": {lpf_id},' in k:
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
    # cada liga se consulta por separado, así que se cuentan los minutos de cada liga (no solo el horario total)
    ventanas = {k: {} for k in PRIORIDAD}
    for clave, (t, d) in list(_memoria.items()):
        if not clave.startswith("hlhoy"):
            continue
        for p in d:
            if p["terminado"]:
                continue
            a, b = max(ahora, p["_inicio"] - VENTANA_ANTES), min(corte, fin_ventana(p))
            if b > a:
                pr = prioridad_de(p, ids)
                cat = "LPF" if pr in ("boca", "river") and p.get("liga") == "LPF" else ("otros" if pr in ("boca", "river") else pr)
                ventanas[cat].setdefault(clave, []).append((a, b))  # el pedido general de la liga
                if pr in ("boca", "river"):
                    ventanas[pr].setdefault(p["id"], []).append((a, b))  # y el pedido aparte del partido

    def union(iv):
        total, fin = 0, 0
        for a, b in sorted(iv):
            a = max(a, fin)
            if b > a:
                total += b - a
                fin = b
        return total / 60
    return {k: sum(union(iv) for iv in v.values()) for k, v in ventanas.items()}


def intervalos():
    """Se recalcula cada 30 segundos (antes se hacía muchas veces por pedido y frenaba el servidor)."""
    if _intervalos["v"] and time.time() - _intervalos["t"] < 30:
        return _intervalos["v"]
    v = _intervalos_calcular()
    _intervalos.update(t=time.time(), v=v)
    return v


def _intervalos_calcular():
    """Reparte los pedidos que quedan en el día: Boca primero, después River, la liga y las copas."""
    restantes = _hl["restantes"] if _hl["restantes"] is not None else HL_LIMITE - _hl["usados"]
    reserva = max(15, (_hl["limite"] or HL_LIMITE) // 4)  # una cuarta parte queda para fichas, goleadores de la lista, formaciones y reinicios
    disponibles = max(0, restantes - reserva)  # una parte queda de reserva para fichas, Europa y planteles
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
    for p in lista:
        p["act"] = int(ahora)  # para que el celular adelante el minuto solo hasta la próxima consulta
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
    # Apertura y Clausura en la misma temporada: primero las tablas del torneo que se está jugando,
    # después la tabla anual y al final las del torneo que ya terminó
    if len(grupos) > 1:
        actual = "clausura" if datetime.now(ARG).month >= 7 else "apertura"
        if cod == "LMX":
            actual = "apertura" if datetime.now(ARG).month >= 7 else "clausura"

        def orden(g):
            n = normalizar(g["nombre"])
            if actual in n:
                return 0
            if any(x in n for x in (" anual ", " annual ", " overall ", " general ", " acumulada ", " promedios ")):
                return 1
            if any(x in n for x in (" apertura ", " clausura ")):
                return 2
            return 1
        grupos.sort(key=orden)
    return {"liga": cod, "nombre": HL_LIGAS[cod][0], "grupos": grupos}


# Países y ciudades que las fuentes mandan en inglés
PAISES_ES = {"Spain": "España", "England": "Inglaterra", "Germany": "Alemania", "France": "Francia", "Italy": "Italia", "Netherlands": "Países Bajos",
             "Holland": "Países Bajos", "Belgium": "Bélgica", "Brazil": "Brasil", "Mexico": "México", "Peru": "Perú", "United States": "Estados Unidos",
             "USA": "Estados Unidos", "Scotland": "Escocia", "Wales": "Gales", "Northern Ireland": "Irlanda del Norte", "Ireland": "Irlanda",
             "Republic of Ireland": "Irlanda", "Switzerland": "Suiza", "Sweden": "Suecia", "Norway": "Noruega", "Denmark": "Dinamarca",
             "Finland": "Finlandia", "Iceland": "Islandia", "Poland": "Polonia", "Czech Republic": "República Checa", "Czechia": "República Checa",
             "Slovakia": "Eslovaquia", "Hungary": "Hungría", "Romania": "Rumania", "Bulgaria": "Bulgaria", "Greece": "Grecia", "Turkey": "Turquía",
             "Türkiye": "Turquía", "Croatia": "Croacia", "Serbia": "Serbia", "Slovenia": "Eslovenia", "Bosnia and Herzegovina": "Bosnia y Herzegovina",
             "Bosnia-Herzegovina": "Bosnia y Herzegovina", "Montenegro": "Montenegro", "North Macedonia": "Macedonia del Norte", "Albania": "Albania",
             "Kosovo": "Kosovo", "Austria": "Austria", "Ukraine": "Ucrania", "Russia": "Rusia", "Georgia": "Georgia", "Armenia": "Armenia",
             "Japan": "Japón", "South Korea": "Corea del Sur", "Korea Republic": "Corea del Sur", "China": "China", "Australia": "Australia",
             "New Zealand": "Nueva Zelanda", "Saudi Arabia": "Arabia Saudita", "Qatar": "Catar", "Iran": "Irán", "Iraq": "Irak", "Israel": "Israel",
             "Egypt": "Egipto", "Morocco": "Marruecos", "Algeria": "Argelia", "Tunisia": "Túnez", "Nigeria": "Nigeria", "Ghana": "Ghana",
             "Senegal": "Senegal", "Ivory Coast": "Costa de Marfil", "Côte d'Ivoire": "Costa de Marfil", "Cote d'Ivoire": "Costa de Marfil",
             "Cameroon": "Camerún", "Mali": "Malí", "Guinea": "Guinea", "South Africa": "Sudáfrica", "DR Congo": "RD del Congo",
             "Congo DR": "RD del Congo", "Gabon": "Gabón", "Cape Verde": "Cabo Verde", "Canada": "Canadá", "Jamaica": "Jamaica",
             "Haiti": "Haití", "Panama": "Panamá", "Costa Rica": "Costa Rica", "Honduras": "Honduras", "Dominican Republic": "República Dominicana",
             "Cuba": "Cuba", "Uruguay": "Uruguay", "Paraguay": "Paraguay", "Chile": "Chile", "Colombia": "Colombia", "Ecuador": "Ecuador",
             "Bolivia": "Bolivia", "Venezuela": "Venezuela", "Argentina": "Argentina", "Portugal": "Portugal", "Luxembourg": "Luxemburgo",
             "Cyprus": "Chipre", "Lithuania": "Lituania", "Latvia": "Letonia", "Estonia": "Estonia", "Belarus": "Bielorrusia",
             "Moldova": "Moldavia", "Azerbaijan": "Azerbaiyán", "Kazakhstan": "Kazajistán", "Uzbekistan": "Uzbekistán", "Syria": "Siria",
             "Jordan": "Jordania", "Lebanon": "Líbano", "United Arab Emirates": "Emiratos Árabes Unidos", "Philippines": "Filipinas",
             "Indonesia": "Indonesia", "Thailand": "Tailandia", "Curacao": "Curazao", "Curaçao": "Curazao", "Suriname": "Surinam",
             "Trinidad and Tobago": "Trinidad y Tobago", "Guatemala": "Guatemala", "El Salvador": "El Salvador", "Nicaragua": "Nicaragua",
             "Europe": "Europa", "South America": "Sudamérica", "World": "Mundo", "Africa": "África", "Asia": "Asia",
             "London": "Londres", "Munich": "Múnich", "Turin": "Turín", "Milan": "Milán", "Rome": "Roma", "Lisbon": "Lisboa", "Seville": "Sevilla",
             "Naples": "Nápoles", "Florence": "Florencia", "Genoa": "Génova", "Marseille": "Marsella", "Cologne": "Colonia", "Athens": "Atenas",
             "Moscow": "Moscú", "Brussels": "Bruselas", "Geneva": "Ginebra", "Vienna": "Viena", "Prague": "Praga", "Warsaw": "Varsovia"}
_PAISES_RE = re.compile(r"(?<![\w])(" + "|".join(sorted((re.escape(k) for k in PAISES_ES), key=len, reverse=True)) + r")(?![\w])")
_CLAVES_PAIS = {"nacionalidad", "ubicacion", "pais", "region", "lugar", "ciudad"}


def pais_es(texto):
    return _PAISES_RE.sub(lambda m: PAISES_ES[m.group(1)], texto) if isinstance(texto, str) and texto else texto


def en_castellano(obj, clave=None):
    """Recorre los datos antes de mandarlos y pasa al castellano los países y ciudades que vienen en inglés."""
    if isinstance(obj, dict):
        return {k: en_castellano(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [en_castellano(v, clave) for v in obj]
    if clave in _CLAVES_PAIS and isinstance(obj, str):
        return pais_es(obj)
    return obj


def _cod_de_partido(id_txt, m=None):
    for c in HL_LIGAS:
        for k, (t, l) in list(_memoria.items()):
            if k.startswith("hlhoy" + c) and any(p["id"] == id_txt for p in l):
                return c
    nombre_liga = normalizar(((m or {}).get("league") or {}).get("name"))
    return next((c for c in HL_LIGAS if _elige(c, nombre_liga)), "LPF")


def _desconocido(nombre):
    n = normalizar(nombre if isinstance(nombre, str) else "").strip()
    return n in ("", "unknown", "desconocido", "n a", "tbd", "null", "none")


def _buscar_puntajes(d):
    """Puntaje (rating) de cada jugador, buscado en lo que devuelva Highlightly, venga como venga."""
    puntajes = {}

    def nombre_de(x):
        for k in ("name", "playerName", "fullName"):
            if isinstance(x.get(k), str):
                return x[k]
        if isinstance(x.get("player"), dict):
            return nombre_de(x["player"])
        return ""

    def valor_rating(x):
        for k in ("rating", "playerRating", "matchRating"):
            if x.get(k) not in (None, "", "-"):
                return x[k]
        for lista in (x.get("statistics"), x.get("stats")):
            if isinstance(lista, list):
                for s in lista:
                    if isinstance(s, dict) and "rating" in str(s.get("name") or s.get("displayName") or s.get("type") or "").lower():
                        return s.get("value")
            if isinstance(lista, dict) and lista.get("rating") is not None:
                return lista["rating"]
        return None

    def mirar(x, prof=0):
        if prof > 8:
            return
        if isinstance(x, list):
            for y in x:
                mirar(y, prof + 1)
        elif isinstance(x, dict):
            r, n = valor_rating(x), nombre_de(x)
            if r is not None and n:
                try:
                    v = float(str(r).replace(",", "."))
                    if 0 < v <= 10:
                        puntajes[normalizar(n).strip()] = round(v, 1)
                except ValueError:
                    pass
            for v in x.values():
                if isinstance(v, (list, dict)):
                    mirar(v, prof + 1)
    mirar(d)
    return puntajes


BANDERAS = {"argentina": "AR", "brazil": "BR", "brasil": "BR", "uruguay": "UY", "paraguay": "PY", "chile": "CL", "colombia": "CO",
            "peru": "PE", "ecuador": "EC", "bolivia": "BO", "venezuela": "VE", "mexico": "MX", "united states": "US", "usa": "US",
            "canada": "CA", "spain": "ES", "espana": "ES", "france": "FR", "germany": "DE", "italy": "IT", "portugal": "PT",
            "netherlands": "NL", "belgium": "BE", "croatia": "HR", "serbia": "RS", "switzerland": "CH", "austria": "AT",
            "denmark": "DK", "sweden": "SE", "norway": "NO", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
            "turkey": "TR", "turkiye": "TR", "greece": "GR", "ukraine": "UA", "russia": "RU", "morocco": "MA", "senegal": "SN",
            "nigeria": "NG", "ghana": "GH", "ivory coast": "CI", "cote d ivoire": "CI", "cameroon": "CM", "egypt": "EG",
            "algeria": "DZ", "tunisia": "TN", "japan": "JP", "south korea": "KR", "korea republic": "KR", "australia": "AU",
            "saudi arabia": "SA", "ireland": "IE", "scotland": "GB-SCT", "england": "GB-ENG", "wales": "GB-WLS",
            "northern ireland": "GB", "united kingdom": "GB", "costa rica": "CR", "panama": "PA", "honduras": "HN",
            "jamaica": "JM", "haiti": "HT", "cuba": "CU", "dominican republic": "DO", "guatemala": "GT", "el salvador": "SV",
            "slovakia": "SK", "slovenia": "SI", "hungary": "HU", "romania": "RO", "bulgaria": "BG", "finland": "FI",
            "iceland": "IS", "albania": "AL", "bosnia and herzegovina": "BA", "north macedonia": "MK", "montenegro": "ME",
            "georgia": "GE", "armenia": "AM", "israel": "IL", "iran": "IR", "qatar": "QA", "china": "CN", "india": "IN",
            "south africa": "ZA", "mali": "ML", "guinea": "GN", "gabon": "GA", "angola": "AO", "cape verde": "CV",
            "dr congo": "CD", "congo dr": "CD", "kosovo": "XK", "luxembourg": "LU", "new zealand": "NZ"}


def _info_jugador(d):
    """Nacionalidad, edad, altura y peso de la ficha de un jugador de Highlightly, venga como venga."""
    x = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    plano = {}

    def juntar(y, prof=0):
        if isinstance(y, dict) and prof < 4:
            for k, v in y.items():
                if isinstance(v, dict):
                    juntar(v, prof + 1)
                elif k not in plano:
                    plano[k] = v
    juntar(x)
    nac = ""
    for fuente in (x, x.get("profile") if isinstance(x.get("profile"), dict) else {}):
        for k in ("nationality", "country", "citizenship"):
            v = fuente.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("code") or ""
            if v and not nac:
                nac = v
    nac = nac or plano.get("nationality") or plano.get("citizenship") or ""
    if isinstance(nac, list):
        nac = nac[0] if nac else ""
    edad = plano.get("age")
    nacimiento = str(plano.get("birthDate") or plano.get("dateOfBirth") or plano.get("birthday") or plano.get("date") or "")[:10]
    if not edad and re.fullmatch(r"\d{4}-\d{2}-\d{2}", nacimiento):
        n = date.fromisoformat(nacimiento)
        hoy = datetime.now(ARG).date()
        edad = hoy.year - n.year - ((hoy.month, hoy.day) < (n.month, n.day))

    def medida(v, unidad):
        m = re.search(r"(\d+(?:[.,]\d+)?)", str(v or ""))
        if not m:
            return ""
        num = float(m.group(1).replace(",", "."))
        if unidad == "m":
            return f"{num / 100:.2f} m" if num > 3 else f"{num:.2f} m"
        return f"{round(num)} kg"
    return {"nacionalidad": nac if isinstance(nac, str) else "", "bandera": BANDERAS.get(normalizar(nac if isinstance(nac, str) else "").strip(), ""),
            "edad": edad, "altura": medida(plano.get("height"), "m"), "peso": medida(plano.get("weight"), "kg")}


def _crudo_fd(id_num):
    """El partido de football-data tal como vino en las listas ya guardadas (para no gastar pedidos)."""
    for k, (t, d) in list(_memoria.items()):
        if k.startswith("/matches?") or k.startswith("/competitions/"):
            for m in (d or {}).get("matches", []) if isinstance(d, dict) else []:
                if str(m.get("id")) == str(id_num):
                    return m
    return None


def resumen_goles(id_txt):
    """Quién hizo los goles y cuántas rojas tuvo cada equipo, para mostrarlo en la lista de partidos."""
    if id_txt.startswith("h"):
        d = datos_partido_hl(id_txt[1:], ligero=True)
        loc = d["local"]["id"]
    else:
        crudo = _crudo_fd(id_txt)
        enc = buscar_en_hl(crudo) if crudo else None
        if not enc:
            return None
        hid, inv = enc
        d = datos_partido_hl(str(hid), ligero=True)
        loc = d["visitante"]["id"] if inv else d["local"]["id"]
    goles = [[g["minuto"], g["jugador"], "L" if g["equipo"] == loc else "V", g["tipo"]] for g in d.get("goles", []) if g.get("jugador")]
    rojas = {"L": 0, "V": 0}
    for e in d.get("eventos", []):
        if re.search(r"red card|second yellow|yellow.?red", e.get("tipo") or "", re.I):
            rojas["L" if e["equipo"] == loc else "V"] += 1
    return {"g": goles, "rL": rojas["L"], "rV": rojas["V"]}


def datos_resumen_goles(ids):
    from concurrent.futures import ThreadPoolExecutor

    def uno(i):
        try:
            return i, resumen_goles(i)
        except Exception:
            return i, None
    with ThreadPoolExecutor(8) as ex:
        return {i: r for i, r in ex.map(uno, ids[:30]) if r}


def hl_muestra(id_num):
    """Revisión: un pedacito de lo que devuelve Highlightly para un partido (para ajustar formación, puntajes y fichas)."""
    def corto(x, n=1500):
        return json.dumps(x, ensure_ascii=False)[:n]
    m = hl(f"/matches/{id_num}", ttl=3600)
    m = m[0] if isinstance(m, list) and m else m
    al = hl(f"/lineups/{id_num}", ttl=3600)
    res = {"eventos": corto((m or {}).get("events", [])[:6]), "formacion": corto(al)}
    try:
        res["boxScore"] = corto(hl(f"/box-score/{id_num}", ttl=3600))
    except Exception as e:
        res["boxScore"] = str(e)
    a0 = al[0] if isinstance(al, list) and al else al
    try:
        j = ((a0 or {}).get("homeTeam") or {}).get("initialLineup")[0][0]
        pid = j.get("id") or j.get("playerId")
        res["jugador"] = corto(hl(f"/players/{pid}", ttl=86400)) if pid else "sin id"
    except Exception as e:
        res["jugador"] = str(e)
    return res


def datos_jugadores_partido(id_num):
    """Ficha (bandera, edad, altura, peso) de los jugadores de un partido. Cada jugador se pide una sola vez por mes."""
    al = hl(f"/lineups/{id_num}", ttl=86400)
    al = al[0] if isinstance(al, list) and al else al
    jugadores = []
    if isinstance(al, dict):
        for lado in ("homeTeam", "awayTeam"):
            x = al.get(lado) or {}
            for fila in x.get("initialLineup") or []:
                jugadores += [j for j in (fila if isinstance(fila, list) else [fila]) if isinstance(j, dict)]
            jugadores += [j for j in x.get("substitutes") or [] if isinstance(j, dict)]
    jugadores = [j for j in jugadores if (j.get("id") or j.get("playerId")) and not _desconocido(j.get("name"))][:50]

    def uno(j):
        pid = j.get("id") or j.get("playerId")
        try:
            return j, _info_jugador(hl(f"/players/{pid}", ttl=30 * 86400))
        except Exception:
            return j, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        res = {}
        for j, info in ex.map(uno, jugadores):
            if info:
                res[normalizar(j.get("name")).strip()] = info
    return {"jugadores": res}


def datos_partido_hl(id_num, ligero=False):
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
    guardado = _memoria.get(clave)
    res["act"] = int(guardado[0]) if guardado and isinstance(guardado[0], (int, float)) else int(time.time())
    res["medio"] = [None, None]
    res["estadio"] = (m.get("venue") or {}).get("name", "") if isinstance(m.get("venue"), dict) else ""
    res["arbitro"] = (m.get("referee") or {}).get("name", "") if isinstance(m.get("referee"), dict) else ""
    eventos = []
    for e in (m.get("events") or []):
        jug = e.get("player")
        jug = (jug.get("name") if isinstance(jug, dict) else jug) or ""
        if _desconocido(jug) and (e.get("type") or "") not in ("Goal", "Own Goal", "Penalty"):
            continue
        eventos.append({"minuto": e.get("time") or "", "tipo": e.get("type") or "", "jugador": "" if _desconocido(jug) else jug,
                        "asistencia": "" if _desconocido(e.get("assist")) else (e.get("assist") or ""),
                        "sale": "" if _desconocido(e.get("substituted")) else (e.get("substituted") or ""),
                        "jugadorId": e.get("playerId") or (e.get("player") or {}).get("id") if isinstance(e.get("player"), dict) else e.get("playerId"),
                        "equipo": f"h{(e.get('team') or {}).get('id')}"})
    res["eventos"] = eventos
    res["goles"] = [{"minuto": e["minuto"], "jugador": e["jugador"], "asistencia": e["asistencia"], "equipo": e["equipo"],
                     "tipo": "OWN" if e["tipo"] == "Own Goal" else "PENALTY" if e["tipo"] == "Penalty" else ""}
                    for e in eventos if e["tipo"] in ("Goal", "Own Goal", "Penalty")]
    res["historial"] = None
    res["fuente"] = "hl"
    res["alineacion"] = None
    if ligero:  # para la lista de partidos alcanza con goles y tarjetas (no se piden formaciones ni puntajes)
        res.pop("_inicio", None)
        return res
    if p["vivo"] or p["terminado"] or (p["_inicio"] - time.time() < 3600):
        try:
            al = hl(f"/lineups/{id_num}", ttl=86400 if p["terminado"] else 900)
            al = al[0] if isinstance(al, list) and al else al

            def jug(j):
                return {"nombre": j.get("name") or "", "numero": j.get("number"), "posicion": j.get("position") or "",
                        "id": j.get("id") or j.get("playerId")}

            def lado(x):
                x = x or {}
                # los jugadores sin nombre ("Unknown") no se muestran
                filas = [[jug(j) for j in (fila if isinstance(fila, list) else [fila]) if isinstance(j, dict) and not _desconocido(j.get("name"))]
                         for fila in (x.get("initialLineup") or [])]
                filas = [f for f in filas if f]
                return {"formacion": x.get("formation") or "", "filas": filas,
                        "suplentes": [jug(j) for j in (x.get("substitutes") or []) if isinstance(j, dict) and not _desconocido(j.get("name"))],
                        "dt": ((x.get("coach") or {}).get("name") if isinstance(x.get("coach"), dict) else x.get("coach")) or ""}
            if isinstance(al, dict) and (al.get("homeTeam") or al.get("awayTeam")):
                a1, a2 = lado(al.get("homeTeam")), lado(al.get("awayTeam"))
                # solo si los dos equipos tienen al menos 7 titulares con nombre
                if sum(len(f) for f in a1["filas"]) >= 7 and sum(len(f) for f in a2["filas"]) >= 7:
                    res["alineacion"] = {"local": a1, "visitante": a2}
        except Exception:
            pass
    if p["terminado"] or p["vivo"]:
        try:
            res["puntajes"] = _buscar_puntajes(hl(f"/box-score/{id_num}", ttl=86400 if p["terminado"] else 600))
        except Exception:
            res["puntajes"] = {}
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


# --- Europa (y el resto de football-data): goles, formaciones y minuto a minuto desde Highlightly ---
_SOBRAN = {"fc", "cf", "afc", "sc", "ac", "club", "de", "the", "calcio", "ssc", "as", "rc", "cd", "ud", "sd", "fk",
           "sv", "bv", "vfb", "vfl", "tsg", "fsv", "1", "1899", "1846", "1848", "1910", "1909", "04", "05", "e", "and", "&"}
_ALIAS = {"utd": "united", "man": "manchester", "atl": "atletico", "internazionale": "inter", "munchen": "munich",
          "monchengladbach": "gladbach", "st": "saint", "psg": "paris"}


def _fichas(nombre):
    return {_ALIAS.get(w, w) for w in normalizar(nombre).split() if w not in _SOBRAN}


def _parecido(nombres_a, nombre_b):
    """0 a 1: cuánto se parecen dos nombres de equipo (por palabras, aceptando abreviaturas)."""
    b = _fichas(nombre_b)
    mejor = 0
    for n in nombres_a:
        a = _fichas(n)
        if not a or not b:
            continue
        chico, grande = (a, b) if len(a) <= len(b) else (b, a)
        ok = sum(1 for w in chico if any(w == x or (len(w) >= 3 and len(x) >= 3 and (x.startswith(w) or w.startswith(x))) for x in grande))
        mejor = max(mejor, ok / len(chico))
    return mejor


def hl_partidos_del_dia(fecha):
    """Todos los partidos del día en Highlightly (se guardan 6 horas: solo sirven para encontrar el número)."""
    lista, offset = [], 0
    for _ in range(25):
        d = hl("/matches", {"date": fecha, "timezone": "America/Argentina/Buenos_Aires", "limit": 100, "offset": offset}, ttl=6 * 3600)
        pagina = _lista(d)
        lista += pagina
        total = ((d or {}).get("pagination") or {}).get("totalCount") if isinstance(d, dict) else None
        offset += 100
        if len(pagina) < 100 or (total is not None and offset >= total):
            break
    return lista


_mapa_hl = {}


def buscar_en_hl(fd):
    """Busca en Highlightly el mismo partido que viene de football-data. Devuelve (id, invertido) o None."""
    if not HL_KEY:
        return None
    if fd.get("id") in _mapa_hl:
        return _mapa_hl[fd["id"]]
    fecha = a_hora_arg(fd["utcDate"]).strftime("%Y-%m-%d")
    loc = [(fd.get("homeTeam") or {}).get(k) or "" for k in ("name", "shortName")]
    vis = [(fd.get("awayTeam") or {}).get(k) or "" for k in ("name", "shortName")]
    mejor, puntaje = None, 0
    for m in hl_partidos_del_dia(fecha):
        h, a = (m.get("homeTeam") or {}).get("name") or "", (m.get("awayTeam") or {}).get("name") or ""
        for inv, (x, y) in ((False, (h, a)), (True, (a, h))):
            pl, pv = _parecido(loc, x), _parecido(vis, y)
            if pl >= .5 and pv >= .5 and pl + pv >= 1.5 and pl + pv > puntaje:
                mejor, puntaje = (m.get("id"), inv), pl + pv
    if mejor:
        _mapa_hl[fd["id"]] = mejor
    return mejor


def sumar_detalle_hl(res, fd):
    """Le agrega a un partido de football-data los goles con nombre, formaciones, minuto a minuto y estadísticas."""
    try:
        enc = buscar_en_hl(fd)
        if not enc:
            return res
        hid, inv = enc
        h = datos_partido_hl(str(hid))
    except Exception:
        return res
    mapa = {h["local"]["id"]: res["visitante"]["id"] if inv else res["local"]["id"],
            h["visitante"]["id"]: res["local"]["id"] if inv else res["visitante"]["id"]}
    for e in h.get("eventos", []):
        e["equipo"] = mapa.get(e["equipo"], e["equipo"])
    for g in h.get("goles", []):
        g["equipo"] = mapa.get(g["equipo"], g["equipo"])
    res["eventos"], res["goles"] = h.get("eventos", []), h.get("goles", []) or res["goles"]
    res["puntajes"] = h.get("puntajes") or {}
    al = h.get("alineacion")
    if al and inv:
        al = {"local": al["visitante"], "visitante": al["local"]}
    res["alineacion"] = al
    res["fuente"], res["hlId"] = "hl", f"h{hid}"
    if h.get("vivo") and h.get("gl") is not None:  # en vivo, Highlightly suele ir más rápido
        gl, gv = (h["gv"], h["gl"]) if inv else (h["gl"], h["gv"])
        res.update(gl=gl, gv=gv, minuto=h.get("minuto") or res.get("minuto"), vivo=True)
    if not res.get("estadio"):
        res["estadio"] = h.get("estadio") or ""
    return res


# --- Planteles armados con las formaciones de los últimos partidos (datos reales y actuales) ---
def plantel_hl(id_txt, partidos=5):
    jugados = []
    for c in HL_LIGAS:
        try:
            jugados += [p for p in hl_temporada(c) if p["terminado"] and id_txt in (p["local"]["id"], p["visitante"]["id"])]
        except Exception:
            continue
    jugados.sort(key=lambda p: -p["_inicio"])
    vistos, plantel = {}, []
    for p in jugados[:partidos]:
        try:
            al = hl(f"/lineups/{p['id'][1:]}", ttl=7 * 86400)
        except Exception:
            continue
        al = al[0] if isinstance(al, list) and al else al
        if not isinstance(al, dict):
            continue
        lado = al.get("homeTeam") if p["local"]["id"] == id_txt else al.get("awayTeam")
        lado = lado or {}
        filas = [f if isinstance(f, list) else [f] for f in (lado.get("initialLineup") or [])]
        for i, fila in enumerate(filas):
            pos = "Goalkeeper" if i == 0 else "Defender" if i == 1 else "Offence" if i == len(filas) - 1 else "Midfield"
            for j in fila:
                if isinstance(j, dict) and j.get("name"):
                    vistos.setdefault(j["name"], (j.get("position") or pos, j.get("number")))
        for j in lado.get("substitutes") or []:
            if isinstance(j, dict) and j.get("name"):
                vistos.setdefault(j["name"], (j.get("position") or "", j.get("number")))
    for n, (pos, num) in vistos.items():
        plantel.append({"id": None, "nombre": n, "posicion": pos, "nacimiento": "",
                        "nacionalidad": f"Camiseta {num}" if num else "", "foto": ""})
    return plantel


_ARRANQUE = time.time()
_tiempos = {}


def _medir(ruta, seg):
    t = _tiempos.setdefault(ruta, {"veces": 0, "ultimo": 0, "maximo": 0, "lentas": 0})
    t["veces"] += 1
    t["ultimo"] = round(seg, 2)
    t["maximo"] = max(t["maximo"], round(seg, 2))
    t["lentas"] += seg > 3


def datos_diagnostico():
    """Para revisar por qué tarda: cuánto tarda cada parte del servidor."""
    ahora = time.time()
    return {"minutosPrendido": round((ahora - _ARRANQUE) / 60, 1),
            "footballDataPedidosUltimoMinuto": len([t for t in _turnos if ahora - t < 60]),
            "highlightlyRestantesHoy": _hl["restantes"],
            "preparadoAlArrancar": {k: f"hace {round(ahora - t)} s" for k, (t, _) in _rapido.items() if k.startswith("hoy")},
            "calendariosListos": len([k for k in _rapido if k.startswith("cal")]),
            "segundosPorPantalla": dict(sorted(_tiempos.items(), key=lambda x: -x[1]["maximo"]))}


def hl_buscar_ligas():
    """Revisión: todo lo que Highlightly tiene de Argentina, buscado de varias formas (se guarda 1 día)."""
    busquedas = [{"countryCode": "AR"}, {"countryName": "Argentina"}, {"leagueName": "Primera B"}, {"leagueName": "Primera B Metropolitana"},
                 {"leagueName": "Metropolitana"}, {"leagueName": "Reserve"}, {"leagueName": "Reserva"}, {"leagueName": "Primera B Nacional"},
                 {"leagueName": "Femenino"}, {"leagueName": "Femenina"}, {"leagueName": "Women"}, {"leagueName": "Primera A"}]
    res = {}
    for b in busquedas:
        nombres, offset = [], 0
        for _ in range(5):
            try:
                d = hl("/leagues", dict(b, limit=100, offset=offset), ttl=86400)
            except Exception as e:
                nombres.append("ERROR: " + str(e))
                break
            ligas = _lista(d)
            for lg in ligas:
                pais = lg.get("country") or {}
                nombres.append(f"{lg.get('name')} · id {lg.get('id')} · {pais.get('name') or pais.get('code') or ''}")
            offset += 100
            if len(ligas) < 100:
                break
        res[" ".join(f"{k}={v}" for k, v in b.items())] = nombres
    return res


def hl_estado():
    return {"clave": bool(HL_KEY), "limiteDelPlan": HL_LIMITE, "reserva": HL_RESERVA, "usadosHoy": _hl["usados"], "restantes": _hl["restantes"], "limite": _hl["limite"],
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
# Contador de visitas (GoatCounter, gratis). Variable GOATCOUNTER = el nombre elegido al registrarse (ej: tikidata)
# Umami (estadísticas de visitas). El número del sitio es público, no es una clave.
UMAMI_ID = re.sub(r"[^a-f0-9-]", "", os.environ.get("UMAMI_ID", "5873f4e9-b106-43e4-b0d4-3c4c967b5d9d").strip().lower())
GOATCOUNTER = re.sub(r"[^a-z0-9-]", "", os.environ.get("GOATCOUNTER", "").strip().lower())
# Solo canales OFICIALES (nombre exacto del canal en YouTube). Si el video no es de uno de estos,
# no se muestra: aparece el botón para buscarlo en YouTube.
CANALES_OFICIALES = {
    # Argentina y Sudamérica
    "espn fans", "espn argentina", "espn deportes", "espn brasil", "espn fc", "espn", "espn america",
    "tnt sports argentina", "tnt sports", "tnt sports brasil", "tyc sports", "dsports", "directv sports",
    "fox sports argentina", "fox sports", "fox sports mx", "disney", "star", "liga profesional de futbol",
    "liga profesional", "afa", "seleccion argentina", "copa argentina", "conmebol", "conmebol libertadores",
    "conmebol sudamericana", "conmebol tv", "ge", "sportv", "premiere", "cazetv", "canal goat", "globo esporte",
    "brasileirao", "liga 1", "liga auf uruguaya", "auf tv", "win sports", "liga betplay dimayor",
    # Europa
    "premier league", "nbc sports", "sky sports", "sky sports football", "sky sports premier league",
    "laliga ea sports", "laliga", "laliga hypermotion", "movistar plus", "dazn", "dazn espana", "dazn football",
    "serie a", "bundesliga", "ligue 1 mcdonald s", "ligue 1", "liga portugal", "eredivisie", "espn nl",
    "uefa", "cbs sports golazo", "cbs sports", "bein sports", "bein sports usa", "paramount", "tnt sports uk",
    "efl", "sky bet championship", "telemundo deportes", "univision deportes", "tudn", "fifa", "fifa world cup",
}

_videos = {}
_sin_video = {}



# ---------------------------------------------------------------------------
# En vivo gratis: partidos que transmiten en vivo canales OFICIALES de YouTube
# (ligas, federaciones y clubes). Se muestran con el reproductor oficial de YouTube.
# Gasta búsquedas de YouTube (100 por día, compartidas con los resúmenes): se busca
# como mucho cada 30 minutos, solo cuando alguien abre la sección, y se guardan 25 para los resúmenes.
# ---------------------------------------------------------------------------
CANALES_VIVO = CANALES_OFICIALES | {
    "boca juniors", "river plate", "racing club", "club atletico independiente", "independiente",
    "san lorenzo", "san lorenzo de almagro", "club atletico huracan", "velez sarsfield", "velez",
    "estudiantes de la plata", "club estudiantes de la plata", "gimnasia y esgrima la plata", "club atletico lanus",
    "club atletico banfield", "argentinos juniors", "rosario central", "club atletico rosario central",
    "newell s old boys", "club atletico newell s old boys", "talleres", "club atletico talleres", "belgrano",
    "club atletico belgrano", "afa estudio", "seleccion argentina", "futbol femenino afa", "liga profesional femenina",
    "primera nacional", "liga federal", "conmebol tv", "kings league", "kings league americas",
}
NO_ES_PARTIDO = [" reaccion", " reaccionando", " watch party", " relato", " previa", " post partido", " postpartido",
                 " radio", " fifa 2", " efootball", " pes 20", " simulacion", " podcast", " conferencia", " sorteo", " charla"]
_vivo = {"t": 0, "lista": []}
_yt_uso = {"dia": "", "busquedas": 0}


def gastar_busqueda_yt(reserva=0):
    """Lleva la cuenta de las búsquedas del día. Devuelve False si no conviene gastar otra."""
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _yt_uso["dia"] != hoy:
        _yt_uso.update(dia=hoy, busquedas=0)
    if _yt_uso["busquedas"] >= 95 - reserva:
        return False
    _yt_uso["busquedas"] += 1
    return True


def datos_en_vivo():
    if not YOUTUBE_KEY:
        return {"partidos": [], "aviso": "sin_clave"}
    if time.time() - _vivo["t"] < 30 * 60:
        return {"partidos": _vivo["lista"], "actualizado": int(_vivo["t"])}
    if not gastar_busqueda_yt(reserva=25):
        return {"partidos": _vivo["lista"], "actualizado": int(_vivo["t"])}
    vistos, lista = set(), []
    for q in ("fútbol en vivo", "partido en vivo"):
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=12, params={
                "part": "snippet", "q": q, "type": "video", "eventType": "live", "maxResults": 50,
                "regionCode": "AR", "relevanceLanguage": "es", "videoEmbeddable": "true", "key": YOUTUBE_KEY})
            items = r.json().get("items", []) if r.status_code == 200 else []
        except Exception:
            items = []
        for it in items:
            s, vid = it.get("snippet", {}), (it.get("id") or {}).get("videoId")
            canal = normalizar(s.get("channelTitle")).strip()
            titulo = s.get("title", "")
            tn = normalizar(titulo)
            if not vid or vid in vistos or canal not in CANALES_VIVO or any(x + " " in tn for x in NO_ES_PARTIDO):
                continue
            vistos.add(vid)
            mini = s.get("thumbnails", {})
            lista.append({"id": vid, "titulo": titulo.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"),
                          "canal": s.get("channelTitle", ""),
                          "portada": (mini.get("high") or mini.get("medium") or {}).get("url") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
        if q == "fútbol en vivo" and not gastar_busqueda_yt(reserva=25):
            break
    _vivo.update(t=time.time(), lista=lista)
    return {"partidos": lista, "actualizado": int(_vivo["t"])}


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
    if time.time() - _sin_video.get(id_partido, 0) < 3 * 3600:
        return respuesta  # ya se buscó hace poco y no estaba: no gastar otra búsqueda (hay 100 por día)
    _sin_video[id_partido] = time.time()
    if not gastar_busqueda_yt():
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
        return pts

    def oficial(it):
        canal = normalizar(it.get("snippet", {}).get("channelTitle")).strip()
        # canal de la lista oficial, o el canal oficial de uno de los dos clubes (nombre exacto)
        return canal in CANALES_OFICIALES or canal in (nl.strip(), nv.strip())
    items = [it for it in items if oficial(it)]
    if items:
        mejor = max(items, key=puntaje)
        s = mejor.get("snippet", {})
        miniaturas = s.get("thumbnails", {})
        foto = (miniaturas.get("maxres") or miniaturas.get("high") or miniaturas.get("medium") or miniaturas.get("default") or {}).get("url", "")
        vid = (mejor.get("id") or {}).get("videoId")
        if vid and puntaje(mejor) >= 4:  # tiene que nombrar a los equipos y ser un resumen
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



_escudos = {}


def escudo_tsdb(nombre):
    """Escudo de un club que no lo trae la API (TheSportsDB). Solo si el nombre coincide de verdad."""
    from urllib.parse import quote
    clave = normalizar(nombre).strip()
    if clave in _escudos:
        return _escudos[clave]
    url = ""
    for variante in (nombre, re.sub(r"\b(FC|CF|AFC|SC|CA|AC|SSC|FK|SK|CD|CSD|CLUB)\b", "", nombre, flags=re.I).strip()):
        for t in primera_lista(tsdb("/searchteams.php?t=" + quote(variante), ttl=30 * 86400)):
            if (t.get("strSport") or "Soccer") != "Soccer" or (t.get("strGender") or "Male").lower() == "female":
                continue
            nombres = [normalizar(x).strip() for x in [t.get("strTeam"), t.get("strTeamShort")] + (t.get("strTeamAlternate") or "").split(",") if x]
            if clave in nombres or any(clave and (clave in n.split(" ") or n == clave) for n in nombres) or \
                    any(n and (n.startswith(clave + " ") or clave.startswith(n + " ")) for n in nombres):
                url = t.get("strBadge") or t.get("strTeamBadge") or ""
                break
        if url:
            break
    _escudos[clave] = url
    return url


def datos_escudos(nombres):
    from concurrent.futures import ThreadPoolExecutor
    lista = [n for n in dict.fromkeys(nombres) if n][:20]
    with ThreadPoolExecutor(5) as ex:
        return dict(zip(lista, ex.map(lambda n: escudo_tsdb(n), lista)))


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
    # --- México (Liga MX) ---
    "club america": ("liso", AM, NA, {"sup": ("liso", NA, AM, {})}),
    "america": ("liso", AM, NA, {"sup": ("liso", NA, AM, {})}),
    "guadalajara": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "chivas": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "cruz azul": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "pumas": ("liso", BL, NA, {"sup": ("liso", NA, ORO, {})}),
    "unam": ("liso", BL, NA, {"sup": ("liso", NA, ORO, {})}),
    "tigres": ("liso", AM, AZ, {"sup": ("liso", AZ, AM, {})}),
    "monterrey": ("rayas", NA, BL, {"m": NA, "sup": ("liso", BL, NA, {})}),
    "toluca": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "santos laguna": ("rayas", VE, BL, {"m": VE, "sup": ("liso", BL, VE, {})}),
    "leon": ("liso", VE, BL, {"sup": ("liso", BL, VE, {})}),
    "pachuca": ("rayas", NA, BL, {"m": NA, "sup": ("liso", BL, NA, {})}),
    "atlas": ("mitad", RO, NE, {"sup": ("liso", BL, RO, {})}),
    "puebla": ("banda", BL, AZ, {"sup": ("liso", AZ, BL, {})}),
    "queretaro": ("rayas", AZ, NE, {"m": AZ, "sup": ("liso", BL, AZ, {})}),
    "necaxa": ("rayas", RO, BL, {"m": RO, "sup": ("liso", BL, RO, {})}),
    "tijuana": ("rayas", RO, NE, {"m": NE, "sup": ("liso", BL, RO, {})}),
    "mazatlan": ("liso", "#5B2C83", BL, {"sup": ("liso", BL, "#5B2C83", {})}),
    "san luis": ("rayas", RO, BL, {"m": RO, "sup": ("liso", AZ, RO, {})}),
    # --- Estados Unidos (MLS) ---
    "inter miami": ("liso", "#F5A9C7", NE, {"sup": ("liso", NE, "#F5A9C7", {})}),
    "la galaxy": ("liso", BL, NA, {"sup": ("liso", NA, ORO, {})}),
    "los angeles fc": ("liso", NE, ORO, {"sup": ("liso", BL, ORO, {})}),
    "lafc": ("liso", NE, ORO, {"sup": ("liso", BL, ORO, {})}),
    "seattle sounders": ("liso", VE, AZ, {"sup": ("liso", AZ, VE, {})}),
    "atlanta united": ("rayas", RO, NE, {"m": NE, "sup": ("liso", BL, RO, {})}),
    "new york city": ("liso", CEL, NA, {"sup": ("liso", NA, CEL, {})}),
    "new york red bulls": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "red bull new york": ("liso", BL, RO, {"sup": ("liso", RO, BL, {})}),
    "orlando city": ("liso", "#5B2C83", BL, {"sup": ("liso", BL, "#5B2C83", {})}),
    "columbus crew": ("liso", AM, NE, {"sup": ("liso", NE, AM, {})}),
    "portland timbers": ("liso", VE, AM, {"sup": ("liso", BL, VE, {})}),
    "toronto": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "cincinnati": ("liso", NA, "#F27C21", {"sup": ("liso", BL, NA, {})}),
    "nashville": ("liso", AM, NA, {"sup": ("liso", NA, AM, {})}),
    "philadelphia union": ("liso", NA, AM, {"sup": ("liso", BL, NA, {})}),
    "houston dynamo": ("liso", "#F27C21", NE, {"sup": ("liso", BL, "#F27C21", {})}),
    "austin": ("liso", VE, NE, {"sup": ("liso", NE, VE, {})}),
    "sporting kansas city": ("liso", CEL, NA, {"sup": ("liso", NA, CEL, {})}),
    "colorado rapids": ("liso", GR, CEL, {"sup": ("liso", BL, GR, {})}),
    "real salt lake": ("liso", GR, AZ, {"sup": ("liso", BL, GR, {})}),
    "dc united": ("liso", NE, RO, {"sup": ("liso", BL, NE, {})}),
    "d c united": ("liso", NE, RO, {"sup": ("liso", BL, NE, {})}),
    "chicago fire": ("liso", RO, BL, {"sup": ("liso", BL, RO, {})}),
    "vancouver whitecaps": ("liso", BL, AZ, {"sup": ("liso", AZ, BL, {})}),
    "new england revolution": ("liso", NA, RO, {"sup": ("liso", BL, NA, {})}),
    "charlotte": ("liso", CEL, NE, {"sup": ("liso", NE, CEL, {})}),
    "st louis city": ("liso", RO, NA, {"sup": ("liso", BL, RO, {})}),
    # --- Arabia Saudita ---
    "al hilal": ("liso", AZ, BL, {"sup": ("liso", BL, AZ, {})}),
    "al nassr": ("liso", AM, AZ, {"sup": ("liso", AZ, AM, {})}),
    "al ittihad": ("rayas", AM, NE, {"m": NE, "sup": ("liso", BL, AM, {})}),
    "al ahli": ("liso", VE, BL, {"sup": ("liso", BL, VE, {})}),
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



# ---------------------------------------------------------------------------
# Importancia de un partido
# nivel 2 = MEGA (Superclásico, finales) · 1 = importante (Boca, River, copas, playoffs, Selección) · 0 = normal
# ---------------------------------------------------------------------------
NO_ES_FINAL = ["semi", "quarter", "cuartos", "octavos", "round of", "1/", "16", "32", "group", "grupo", "repechaje", "play-off round"]


def _es_final(p):
    f = normalizar(p.get("fase") or "")
    return " final " in f and not any(x in f for x in NO_ES_FINAL)


def nivel_partido(p):
    nl, nv = normalizar(p["local"]["nombre"]), normalizar(p["visitante"]["nombre"])
    argentino = p["liga"] in ("LPF", "CAR", "LIB", "SUD")
    boca = argentino and (" boca juniors " in nl or " boca juniors " in nv)
    river = argentino and (" river plate " in nl or " river plate " in nv)
    if (boca and river) or _es_final(p):
        return 2, ("Superclásico" if boca and river else "Final")
    if boca or river:
        return 1, "Boca" if boca else "River"
    if p["liga"] in ("LIB", "SUD") and p.get("fase"):
        return 1, "Copa"
    if p["liga"] in ("LPF", "CAR") and p.get("fase"):
        return 1, "Playoffs" if p["liga"] == "LPF" else "Copa Argentina"
    if p["liga"] == "WC" and (" argentina " in nl or " argentina " in nv):
        return 1, "Selección"
    return 0, ""


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


def I(fundacion, estadio, ubicacion, apodo, capacidad=None):
    return {"fundacion": fundacion, "estadio": estadio, "ubicacion": ubicacion, "apodo": apodo, "capacidad": capacidad}


# Datos de los clubes argentinos cargados a mano y revisados uno por uno.
# Si un dato no es seguro, no se pone (mejor nada que algo equivocado).
CLUBES_AR = {
    "boca juniors": I(1905, "Alberto J. Armando (La Bombonera)", "La Boca, Buenos Aires", "Xeneize", 57200),
    "river plate": I(1901, "Más Monumental", "Núñez, Buenos Aires", "Millonario", 84500),
    "racing": I(1903, "Presidente Perón (El Cilindro)", "Avellaneda, Buenos Aires", "La Academia", 51000),
    "independiente": I(1905, "Libertadores de América - Ricardo Enrique Bochini", "Avellaneda, Buenos Aires", "Rojo"),
    "san lorenzo": I(1908, "Pedro Bidegain (Nuevo Gasómetro)", "Bajo Flores, Buenos Aires", "Ciclón", 47900),
    "huracan": I(1908, "Tomás Adolfo Ducó", "Parque Patricios, Buenos Aires", "Globo", 48300),
    "velez": I(1910, "José Amalfitani", "Liniers, Buenos Aires", "Fortín", 49500),
    "estudiantes": I(1905, "Jorge Luis Hirschi (UNO)", "La Plata, Buenos Aires", "Pincharrata"),
    "gimnasia": I(1887, "Juan Carmelo Zerillo (El Bosque)", "La Plata, Buenos Aires", "Lobo"),
    "lanus": I(1915, "Ciudad de Lanús - Néstor Díaz Pérez (La Fortaleza)", "Lanús, Buenos Aires", "Granate", 47000),
    "banfield": I(1896, "Florencio Sola", "Banfield, Buenos Aires", "Taladro", 34900),
    "argentinos juniors": I(1904, "Diego Armando Maradona", "La Paternal, Buenos Aires", "Bicho"),
    "rosario central": I(1889, "Gigante de Arroyito", "Rosario, Santa Fe", "Canalla", 41600),
    "newell": I(1903, "Marcelo Bielsa (Coloso del Parque)", "Rosario, Santa Fe", "Lepra", 42000),
    "newells": I(1903, "Marcelo Bielsa (Coloso del Parque)", "Rosario, Santa Fe", "Lepra", 42000),
    "talleres": I(1913, "Mario Alberto Kempes", "Córdoba", "Matador", 57000),
    "belgrano": I(1905, "Julio César Villagra (Gigante de Alberdi)", "Córdoba", "Pirata"),
    "instituto": I(1918, "Juan Domingo Perón (Monumental de Alta Córdoba)", "Córdoba", "La Gloria"),
    "godoy cruz": I(1921, "Malvinas Argentinas", "Mendoza", "Tomba"),
    "independiente rivadavia": I(1913, "Bautista Gargantini", "Mendoza", "Lepra mendocina"),
    "gimnasia mendoza": I(1908, "Víctor Antonio Legrotaglie", "Mendoza", "Lobo mendocino"),
    "gimnasia y esgrima mendoza": I(1908, "Víctor Antonio Legrotaglie", "Mendoza", "Lobo mendocino"),
    "atletico tucuman": I(1902, "Monumental José Fierro", "San Miguel de Tucumán", "Decano", 35200),
    "central cordoba": I(1919, "Único Madre de Ciudades", "Santiago del Estero", "Ferroviario", 30000),
    "defensa y justicia": I(1935, "Norberto Tomaghello", "Florencio Varela, Buenos Aires", "Halcón"),
    "platense": I(1905, "Ciudad de Vicente López", "Vicente López, Buenos Aires", "Calamar"),
    "tigre": I(1902, "José Dellagiovanna", "Victoria, Buenos Aires", "Matador de Victoria", 26200),
    "sarmiento": I(1911, "Eva Perón", "Junín, Buenos Aires", "Verde"),
    "barracas central": I(1904, "Claudio Chiqui Tapia", "Barracas, Buenos Aires", "Guapo"),
    "riestra": I(1931, "Guillermo Laza", "Nueva Pompeya, Buenos Aires", "Malevo"),
    "aldosivi": I(1913, "José María Minella", "Mar del Plata, Buenos Aires", "Tiburón", 35000),
    "union": I(1907, "15 de Abril", "Santa Fe", "Tatengue", 28000),
    "san martin de san juan": I(1907, "Ingeniero Hilario Sánchez", "San Juan", "Verdinegro"),
    "san martin sj": I(1907, "Ingeniero Hilario Sánchez", "San Juan", "Verdinegro"),
    "san martin s j": I(1907, "Ingeniero Hilario Sánchez", "San Juan", "Verdinegro"),
    "estudiantes rio cuarto": I(1912, "Ciudad de Río Cuarto (Antonio Candini)", "Río Cuarto, Córdoba", "León del Imperio"),
    "san martin de tucuman": I(1909, "La Ciudadela", "San Miguel de Tucumán", "Santo"),
    "colon": I(1905, "Brigadier General Estanislao López (Cementerio de los Elefantes)", "Santa Fe", "Sabalero"),
    "chacarita": I(1906, "Chacarita Juniors", "Villa Maipú, San Martín", "Funebrero"),
    "quilmes": I(1887, "Centenario", "Quilmes, Buenos Aires", "Cervecero"),
    "ferro carril oeste": I(1904, "Arquitecto Ricardo Etcheverri", "Caballito, Buenos Aires", "Verdolaga"),
    "atlanta": I(1904, "Don León Kolbowski", "Villa Crespo, Buenos Aires", "Bohemio"),
    "all boys": I(1913, "Islas Malvinas", "Floresta, Buenos Aires", "Albo"),
    "nueva chicago": I(1911, "Republica de Mataderos", "Mataderos, Buenos Aires", "Torito"),
    "almirante brown": I(1922, "Fragata Presidente Sarmiento", "Isidro Casanova, Buenos Aires", "Mirasol"),
    "temperley": I(1912, "Alfredo Beranger", "Temperley, Buenos Aires", "Gasolero"),
    "patronato": I(1914, "Presbítero Bartolomé Grella", "Paraná, Entre Ríos", "Santo"),
    "arsenal sarandi": I(1957, "Julio Humberto Grondona", "Sarandí, Buenos Aires", "Arse"),
    "arsenal de sarandi": I(1957, "Julio Humberto Grondona", "Sarandí, Buenos Aires", "Arse"),
}
_CLAVES_AR = sorted(CLUBES_AR, key=len, reverse=True)
# palabras que no sirven para comparar nombres de estadios
_COMUNES_ESTADIO = {"estadio", "stadium", "stadion", "club", "municipal", "ciudad", "arena", "park", "ground", "nuevo", "nueva"}


def info_club_ar(nombres):
    for n in nombres:
        tn = normalizar(n)
        for clave in _CLAVES_AR:
            if all(" " + w + " " in tn for w in clave.split() if w not in ("de", "del", "y", "la")):
                return CLUBES_AR[clave]
    return None


def _mismo_estadio(a, b):
    """True si los dos nombres de estadio comparten alguna palabra importante (para no mostrar fotos de otro estadio)."""
    pa = {w for w in normalizar(a).split() if len(w) > 3 and w not in _COMUNES_ESTADIO}
    pb = {w for w in normalizar(b).split() if len(w) > 3 and w not in _COMUNES_ESTADIO}
    return bool(pa & pb)


def datos_club_extra(id_equipo):
    """Solo datos confirmados: los argentinos cargados a mano, los de football-data para el resto.
    La foto del estadio y el plantel de TheSportsDB se usan solo si se comprueba que son del mismo club."""
    id_txt = str(id_equipo)
    if id_txt.startswith("h"):
        n = hl_equipo(id_txt)["nombre"]
        t = {"shortName": n, "name": n, "squad": None}
    else:
        t = api(f"/teams/{id_equipo}", ttl=86400)
    nombres = [t.get("shortName"), t.get("name")]
    argentino = es_argentino(id_txt)
    res = {"capacidad": None, "ubicacion": "", "fundacion": None, "estadio": "", "descripcion": "",
           "fotoEstadio": "", "camisetas": [], "fotos": {}, "apodo": "", "dt": "", "wikipedia": ""}
    info = info_club_ar(nombres) if argentino else None
    if info:
        res.update(info)
    estadio_ref = (info or {}).get("estadio") or t.get("venue") or ""
    extra = buscar_equipo_tsdb(nombres) or {}
    pais_ok = (not argentino) or (extra.get("strCountry") or "").lower() == "argentina"
    verificado = bool(extra) and pais_ok and estadio_ref and _mismo_estadio(extra.get("strStadium") or "", estadio_ref)
    if verificado:
        venue = {}
        if extra.get("idVenue"):
            venue = (primera_lista(tsdb(f"/lookupvenue.php?id={extra['idVenue']}")) or [{}])[0]
        res["fotoEstadio"] = venue.get("strThumb") or extra.get("strStadiumThumb") or ""
        if not res["capacidad"]:
            res["capacidad"] = extra.get("intStadiumCapacity") or venue.get("intCapacity")
    res["camisetas"] = camisetas_pixel(nombres, argentino, t.get("clubColors") or "")
    if id_txt.startswith("h") and HL_KEY:
        try:
            res["plantel"] = plantel_hl(id_txt)
            if res["plantel"]:
                res["plantelNota"] = "Jugadores de los últimos partidos"
        except Exception:
            pass
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


def noticias_sin_esperar():
    """Las noticias nunca hacen esperar: si están viejas se muestran igual y se actualizan por detrás;
    si todavía no hay ninguna, se empiezan a buscar y la página vuelve a preguntar en unos segundos."""
    viejas = _noticias["lista"]
    if viejas and time.time() - _noticias["t"] < 900:
        return viejas
    if not _candado_noticias.locked():
        threading.Thread(target=traer_todas, daemon=True).start()
    return viejas or None


def datos_noticias(prefs=""):
    grupos = noticias_sin_esperar()
    if grupos is None:
        return {"destacadas": [], "noticias": [], "temas": [], "preparando": True}
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
<meta name="description" content="Resultados en vivo, avisos de gol, tablas y noticias de fútbol. Gratis.">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Tiki Data">
<meta property="og:title" content="Tiki Data — El fútbol en datos">
<meta property="og:description" content="Resultados en vivo, avisos de gol, tablas y noticias de fútbol. Gratis.">
<meta property="og:url" content="__BASE__/">
<meta property="og:image" content="__BASE__/compartir.jpg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="__BASE__/compartir.jpg">
<link rel="icon" href="/logo.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#05070D">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Tiki Data">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
__CONTADOR__
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
.fila { display:grid; grid-template-columns:50px minmax(0,1fr); align-items:stretch; border-top:1px solid var(--linea); }
.fila .st { display:flex; align-items:center; justify-content:center; border-right:1px solid var(--linea); padding:0 2px; }
.fila-c { display:flex; flex-direction:column; min-width:0; }
.fila-top { display:grid; grid-template-columns:minmax(0,1fr) 62px minmax(0,1fr); align-items:center; gap:6px; padding:11px 8px; }
.fila-goles { display:grid; grid-template-columns:1fr 1fr; border-top:1px solid var(--linea); }
.fila-goles:empty { display:none; }
.fila-goles .fg { display:flex; flex-direction:column; align-items:flex-start; font-size:11.5px; color:#AEBBD0; line-height:1.35; min-width:0; }
.fila-goles .fg > span { display:block; width:100%; padding:5px 10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.fila-goles .fg > span + span { border-top:1px solid var(--linea); }
.fila-goles .fg.izq > span { text-align:right; }
.fila-goles .fg + .fg { border-left:1px solid var(--linea); }
.fila-goles em { font-style:normal; color:#3DD68C; font-weight:700; }
.fg-roja { display:inline-flex; gap:2px; align-items:center; } .fg-roja i { display:block; width:7px; height:10px; border-radius:1.5px; background:#FF3B3B; }
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
.cargando { padding:34px 24px 30px; text-align:center; color:var(--suave); font-weight:600; display:flex; flex-direction:column; align-items:center; gap:10px; }
/* Mientras carga: la pelota entra en el arco */
.anim-gol { position:relative; width:170px; height:74px; margin-bottom:6px; }
.anim-gol .piso { position:absolute; left:0; right:0; bottom:0; height:3px; border-radius:2px; background:linear-gradient(90deg, transparent, #1E7A43 15%, #1E7A43 85%, transparent); }
.anim-gol .arco { position:absolute; right:6px; bottom:3px; width:52px; height:38px; border:3px solid #EEF2F7; border-bottom:0; border-radius:3px 3px 0 0;
  background:repeating-linear-gradient(0deg, rgba(255,255,255,.18) 0 1px, transparent 1px 6px), repeating-linear-gradient(90deg, rgba(255,255,255,.18) 0 1px, transparent 1px 6px);
  transform-origin:50% 100%; animation:red 2s ease-in-out infinite; }
.anim-gol .bola { position:absolute; left:4px; bottom:3px; width:16px; height:16px; border-radius:50%;
  background:radial-gradient(circle at 50% 50%, #0D1626 0 20%, transparent 22%), radial-gradient(circle at 15% 35%, #0D1626 0 12%, transparent 14%), radial-gradient(circle at 85% 35%, #0D1626 0 12%, transparent 14%), radial-gradient(circle at 50% 95%, #0D1626 0 14%, transparent 16%), radial-gradient(circle at 35% 30%, #fff, #C9D6E8 75%);
  animation:tiro 2s cubic-bezier(.3,.6,.4,1) infinite; }
.anim-gol .txt-gol { position:absolute; right:10px; top:2px; font-size:15px; font-weight:900; color:#3DD68C; letter-spacing:1px; animation:grito 2s ease infinite; }
@keyframes tiro { 0% { transform:translate(0,0) rotate(0); opacity:1; } 30% { transform:translate(62px,-42px) rotate(300deg); }
  55% { transform:translate(128px,-16px) rotate(620deg); } 70% { transform:translate(132px,-4px) rotate(680deg); } 88% { transform:translate(132px,0) rotate(700deg); opacity:1; } 100% { transform:translate(132px,0) rotate(700deg); opacity:0; } }
@keyframes red { 0%, 52% { transform:scale(1); } 60% { transform:scale(1.08, 1.05); } 70% { transform:scale(.98); } 80%, 100% { transform:scale(1); } }
@keyframes grito { 0%, 55% { opacity:0; transform:translateY(8px) scale(.6); } 65% { opacity:1; transform:translateY(0) scale(1.15); } 90% { opacity:1; transform:scale(1); } 100% { opacity:0; } }
.dato-carga { max-width:300px; font-size:13px; font-weight:600; color:#C7D2E0; line-height:1.45; animation:aparece .4s ease; }
.dato-carga b { color:#4F8FF7; display:block; font-size:11px; letter-spacing:1px; margin-bottom:2px; }
@keyframes aparece { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
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
nav.abajo div { max-width:560px; margin:0 auto; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); padding:6px 4px 14px; }
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
.logo-liga { width:26px; height:26px; flex:none; border-radius:7px; background:#EEF2F7; padding:3px; object-fit:contain; display:inline-block; }
.logo-liga.vacio { background:none; padding:0; display:inline-flex; align-items:center; justify-content:center; }
.meses { display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:6px; }
.mes { min-height:38px; border-radius:9px; border:0; background:var(--sup); color:#C7D2E0; font-size:12px; font-weight:700; }
.mes.on { background:var(--azul); color:var(--fondo); }
.cdia .cant { font-style:normal; font-size:10px; font-weight:800; color:#8FB8FA; }
.cdia.on .cant { color:#070B14; }
.cal-dia { display:flex; justify-content:space-between; align-items:baseline; padding:4px 2px 0; } .cal-dia b { font-size:16px; } .cal-dia small { color:var(--suave); font-size:12px; font-weight:700; }
.cal-liga { border-left:4px solid var(--cl); }
.cab-liga { display:flex; flex-direction:column; gap:2px; padding:11px 12px 9px; border-bottom:1px solid var(--linea); background:rgba(255,255,255,.02); }
.cab-liga b { font-size:14px; } .cab-liga small { font-size:11.5px; color:var(--suave); font-weight:700; }
.cal { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:4px; }
.cal .dsem { text-align:center; font-size:11px; font-weight:700; color:var(--suave); padding-bottom:4px; }
.cdia { height:46px; border-radius:10px; border:0; background:transparent; color:#7A8799; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; font-size:14px; font-weight:700; }
.cdia.con { background:var(--sup); color:var(--texto); }
.cdia.hoy { outline:1.5px solid var(--azul); }
.cdia.on { background:var(--azul); color:var(--fondo); }
.cdia .ps { display:flex; gap:2px; height:12px; align-items:center; } .cdia .ps i { width:5px; height:5px; border-radius:3px; }
.marcador { display:grid; grid-template-columns:1fr auto 1fr; gap:8px; align-items:start; padding:14px; background:var(--negro); }
.marcador .lado { display:flex; flex-direction:column; align-items:center; gap:8px; text-align:center; font-weight:600; font-size:14px; }
.marcador .lado img, .marcador .lado .esc { width:58px; height:58px; }
.marcador .res { font-family:Montserrat, sans-serif; font-weight:900; font-size:40px; text-align:center; padding-top:6px; font-variant-numeric:tabular-nums; }
.marcador .estado { text-align:center; font-size:13px; font-weight:700; color:var(--suave); }
.marcador .estado.vivo { color:var(--vivo); }
/* Ficha del partido */
.marcador { grid-template-columns:1fr auto 1fr; row-gap:4px; }
.esc-rojas { position:relative; display:inline-flex; }
.rojas { position:absolute; right:-14px; top:0; display:flex; flex-direction:column; gap:3px; }
.rojas i { display:block; width:9px; height:13px; border-radius:2px; background:#FF3B3B; box-shadow:0 0 0 1px rgba(0,0,0,.4); }
.goles-m { font-size:12.5px; color:#C7D2E0; line-height:1.55; padding-top:6px; border-top:1px solid var(--linea); }
.goles-m b { color:var(--texto); }
.goles-m.izq { text-align:right; } .goles-m.der { text-align:left; } .goles-m.pelota { text-align:center; font-size:12px; opacity:.7; }
.tj { display:inline-block; width:10px; height:14px; border-radius:2px; background:#F5C400; vertical-align:-2px; box-shadow:0 0 0 1px rgba(0,0,0,.35); }
.tj.roja { background:#FF3B3B; }
.cam { width:42px; height:38px; display:block; filter:drop-shadow(0 2px 3px rgba(0,0,0,.45)); }
.eq-barra { display:flex; align-items:center; gap:10px; padding:10px 14px; background:var(--sup); font-size:14px; }
.eq-barra img, .eq-barra .esc { width:26px; height:26px; }
.eq-barra span { margin-left:auto; font-weight:900; font-family:Montserrat, sans-serif; }
.cancha1 { position:relative; padding:10px 4px; background:repeating-linear-gradient(0deg, #0E3B24 0 44px, #0C3520 44px 88px); }
.cancha1::before { content:''; position:absolute; inset:8px; border:2px solid rgba(255,255,255,.18); border-radius:4px; pointer-events:none; }
.mitad { display:flex; flex-direction:column; gap:14px; padding:10px 0; }
.linea-medio { position:relative; height:2px; background:rgba(255,255,255,.22); margin:4px 8px; }
.linea-medio::after { content:''; position:absolute; left:50%; top:50%; width:72px; height:72px; margin:-36px 0 0 -36px; border:2px solid rgba(255,255,255,.18); border-radius:50%; }
.cl { display:flex; justify-content:space-around; position:relative; z-index:1; }
.fj { display:flex; flex-direction:column; align-items:center; gap:3px; width:68px; }
.fj-cam { position:relative; }
.fj-nom { font-size:11px; font-weight:700; color:#EEF2F7; text-shadow:0 1px 2px #000; max-width:72px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fj-tj { position:absolute; right:-6px; top:-2px; }
.fj-gol { position:absolute; left:-12px; bottom:-2px; font-size:11px; letter-spacing:-3px; }
.fj-cambio { position:absolute; right:-10px; bottom:-4px; min-width:18px; height:18px; padding:0 3px; border-radius:9px; background:#0B1220; border:1.5px solid #EEF2F7; color:#EEF2F7; font-size:10px; font-weight:800; display:flex; align-items:center; justify-content:center; }
.pje { position:absolute; left:-14px; top:-6px; font-size:10px; font-weight:900; color:#06140C; padding:1px 4px; border-radius:6px; }
.fjug .pje { position:static; display:inline-block; font-size:12px; padding:3px 7px; margin-bottom:4px; }
.fjug { display:grid; grid-template-columns:44px 1fr auto; gap:12px; align-items:center; padding:10px 12px; border-top:1px solid var(--linea); }
.num-c { position:relative; width:38px; height:38px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:900; font-size:14px; border:2px solid; }
.num-c.dt { background:#3A4458; color:#EEF2F7; border-color:#5B6B82; font-size:12px; }
.bandera { position:absolute; left:-6px; top:-6px; font-size:15px; font-style:normal; line-height:1; }
.fjug-t { display:flex; flex-direction:column; min-width:0; }
.fjug-t small { font-size:11.5px; color:var(--suave); } .fjug-t b { font-size:14.5px; }
.mks { display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }
.mk { font-size:11.5px; font-weight:700; color:#C7D2E0; background:rgba(255,255,255,.05); padding:2px 7px; border-radius:6px; display:inline-flex; align-items:center; gap:4px; }
.mk.sale { color:#EF4444; } .mk.entra { color:#22C55E; } .mk.asis { color:#8FB8FA; }
.fjug-d { text-align:right; } .fjug-d small { font-size:12px; color:var(--suave); font-weight:700; line-height:1.45; }
.chip img { width:18px; height:18px; object-fit:contain; vertical-align:-4px; }
.mini-eq { display:inline-flex; align-items:center; gap:6px; } .mini-eq img { width:20px; height:20px; object-fit:contain; }
.mm2 { overflow:hidden; }
.mm2-cab { display:grid; grid-template-columns:1fr 1fr; border-bottom:1px solid var(--linea); }
.mm2-eq { display:flex; align-items:center; gap:8px; padding:12px 14px; font-size:13.5px; }
.mm2-eq.der { justify-content:flex-end; text-align:right; }
.mm2-eq img { width:22px; height:22px; object-fit:contain; }
.mm2-cuerpo { position:relative; padding:4px 0 8px; }
.mm2-cuerpo::before { content:''; position:absolute; left:50%; top:0; bottom:0; width:1px; background:#24324A; }
.mm2-fila { position:relative; display:grid; grid-template-columns:1fr 44px 1fr; align-items:center; padding:7px 8px; border-bottom:1px solid rgba(255,255,255,.035); }
.mm2-min { justify-self:center; z-index:1; min-width:38px; text-align:center; font-size:11.5px; font-weight:700; font-variant-numeric:tabular-nums; color:#C7D2E0; background:var(--tarj); padding:2px 4px; }
.mm2-card { display:flex; align-items:center; gap:9px; padding:2px 4px; min-width:0; }
.mm2-card.izq { flex-direction:row-reverse; text-align:right; }
.mm2-card.izq small { justify-content:flex-end; }
.mm2-ic { flex:none; width:18px; height:18px; opacity:.95; } .mm2-ic svg { width:18px; height:18px; display:block; }
.mm2-emo { display:block; font-size:15px; line-height:18px; text-align:center; }
.mm2-ic:has(.mm2-palo) { width:38px; height:24px; } .mm2-palo { position:relative; display:block; width:38px; height:24px; } .mm2-ic .mm2-palo svg, .mm2-palo svg { width:38px; height:24px; display:block; } .mm2-palo i { position:absolute; left:25.6px; top:6.5px; font-style:normal; font-size:12px !important; line-height:12px; width:12px; height:12px; }
.mm2-txt { display:flex; flex-direction:column; min-width:0; }
.mm2-txt small { display:flex; gap:6px; align-items:center; font-size:10px; font-weight:700; letter-spacing:.6px; text-transform:uppercase; color:#7F8CA3; }
.mm2-nom { font-size:13px; font-weight:600; color:#E6EBF2; line-height:1.3; overflow-wrap:anywhere; }
.mm2-card.gol .mm2-nom { font-weight:800; color:#FFFFFF; }
.mm2-extra { font-size:11.5px; color:#8E9AAE; margin-top:1px; }
.mm2-in, .mm2-out { font-size:10px; font-weight:900; } .mm2-in { color:#22C55E; } .mm2-out { color:#EF4444; }
.item.proximo { opacity:.6; cursor:default; }
.tarjeta.proxima { opacity:.7; } .tarjeta.proxima .cab { gap:10px; } .tag-prox { margin-left:auto; font-style:normal; font-size:10px; font-weight:800; letter-spacing:.3px; color:#C9CFD8; background:#1B2E4F; border-radius:20px; padding:3px 8px; }
.mm2-res { font-size:10.5px; font-weight:800; color:#C7D2E0; border:1px solid #3A4A66; border-radius:4px; padding:0 5px; letter-spacing:0; font-variant-numeric:tabular-nums; }
.mm2-sep { position:relative; z-index:1; text-align:center; margin:6px 0; }
.mm2-sep span { display:inline-block; font-size:10px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase; color:#8E9AAE; background:var(--tarj); padding:4px 12px; }
.mm2-sep.fin span, .mm2-sep.vivo span { color:#E6EBF2; }
.mm-cab { display:flex; justify-content:space-between; padding:12px 14px; border-bottom:1px solid var(--linea); font-size:13.5px; }
.mm-fila { display:grid; grid-template-columns:1fr 46px 1fr; align-items:center; gap:6px; padding:8px 10px; border-top:1px solid rgba(255,255,255,.04); }
.mm-min { text-align:center; font-size:12px; font-weight:900; color:var(--texto); background:var(--sup); border-radius:999px; padding:4px 0; }
.mm-ev { display:flex; align-items:center; gap:8px; font-size:13px; }
.mm-ev.izq { flex-direction:row-reverse; text-align:right; }
.mm-ev small { display:block; font-size:11px; color:var(--suave); font-weight:700; }
.mm-ev .mm-det small { color:#8FB8FA; font-weight:600; }
.mm-ic { flex:none; width:24px; text-align:center; font-size:16px; }
.mm-ev .entra { color:#3DD68C; font-weight:700; } .mm-ev .sale { color:#FF7B7B; font-weight:700; }
.var { font-size:9px; font-weight:900; background:#EEF2F7; color:#0B1220; padding:2px 3px; border-radius:3px; }
.mm-sep { text-align:center; font-size:11px; font-weight:800; letter-spacing:1px; color:var(--suave); padding:8px; text-transform:uppercase; background:rgba(255,255,255,.02); }

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
.tabla td.zpos { box-shadow:inset 3px 0 0 var(--z); padding-left:6px; }
.tabla tr.corte td { border-top:1px solid rgba(255,255,255,.22); }
.tabla tr.corte-desc td { border-top:1.5px dashed #EF4444; }
.tabla td.prox { padding:4px 6px 4px 2px; } .tabla td.prox a { display:inline-flex; flex-direction:column; align-items:center; gap:1px; }
.tabla td.prox small { font-size:9px; font-weight:800; color:var(--suave); line-height:1; }
.esc.mini, img.esc.mini { width:20px !important; height:20px !important; font-size:7px !important; }
.zonas { display:flex; flex-wrap:wrap; gap:6px 14px; padding:10px 12px 12px; border-top:1px solid var(--linea); font-size:11.5px; color:#AEBBD0; }
.zonas span { display:inline-flex; align-items:center; gap:6px; } .zonas i { width:10px; height:10px; border-radius:3px; display:block; }
.po .cab small { color:var(--suave); font-size:11px; }
.po-etis, .po-cuadro { display:grid; gap:4px; padding:0 8px; }
.po-etis { padding-top:4px; } .po-etis span { font-size:9.5px; font-weight:800; color:var(--suave); text-align:center; text-transform:uppercase; letter-spacing:.2px; }
.po-cuadro { padding-bottom:14px; min-height:220px; }
.po-col { display:flex; flex-direction:column; justify-content:space-around; gap:8px; min-width:0; }
.po-llave { position:relative; display:flex; flex-direction:column; background:var(--tarj2); border:1px solid var(--linea); border-radius:8px; padding:3px 4px; }
.po-llave.izq::after, .po-llave.der::before { content:''; position:absolute; top:50%; width:4px; border-top:1px solid #3A4D6E; }
.po-llave.izq::after { right:-5px; } .po-llave.der::before { left:-5px; }
.po-eq { display:flex; align-items:center; justify-content:space-between; gap:2px; height:22px; }
.po-eq b { font-size:12px; font-weight:800; min-width:9px; text-align:right; }
.po-eq.gana b { color:#3DD68C; } .po-eq.pierde { opacity:.45; }
.po-nd { width:20px; height:20px; border-radius:10px; background:#1B2E4F; color:#6F86A8; font-size:10px; font-weight:800; display:inline-flex; align-items:center; justify-content:center; }
.po-centro { align-items:center; justify-content:center; gap:6px; }
.po-centro .po-llave { border-color:#E8B33A; width:100%; }
.po-copa { font-size:24px; line-height:1; }
.po-camp { font-size:9.5px; color:var(--suave); text-align:center; line-height:1.25; } .po-camp b { color:#E8B33A; font-size:10.5px; }
.jug { display:grid; grid-template-columns:24px 40px 1fr auto; align-items:center; gap:10px; padding:10px 12px; border-top:1px solid var(--linea); }
.avatar.pos-av, .pos-av { font-size:12px; background:var(--c) !important; color:#0B1220 !important; align-items:center !important; font-weight:900 !important; letter-spacing:.3px; box-shadow:inset 0 -3px 0 rgba(0,0,0,.18); }
.avatar.xl.pos-av { font-size:24px; }
.silueta { width:80%; height:80%; margin-bottom:-2px; opacity:.85; }
.avatar { width:40px; height:40px; border-radius:20px; background:#1B2E4F; color:#6F86A8; overflow:hidden; align-items:flex-end !important; font-weight:800; font-size:13px; display:flex; align-items:center; justify-content:center; }
.esc-jug { width:40px; height:40px; display:flex; align-items:center; justify-content:center; flex:none; }
.esc-jug .esc { width:34px !important; height:34px !important; object-fit:contain; font-size:11px; }
.esc-jug.xl { width:84px; height:84px; } .esc-jug.xl .esc { width:72px !important; height:72px !important; font-size:20px; }
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
.cats-trivia { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.cat-trivia { display:flex; flex-direction:column; align-items:center; gap:6px; padding:16px 10px; border:1px solid var(--borde); text-align:center; }
.cat-trivia b { font-size:14px; } .cat-trivia small { font-size:11px; color:var(--suave); }
.cat-trivia:active, .opcion-trivia:active { transform:scale(.98); }
.niveles-trivia { display:grid; grid-template-columns:repeat(2, 1fr); gap:8px; margin-top:10px; }
.nivel-trivia { display:flex; flex-direction:column; align-items:center; gap:2px; padding:10px 4px; border-radius:10px; border:1.5px solid var(--borde); background:var(--sup); font-weight:800; font-size:13px; }
.nivel-trivia small { font-size:10px; font-weight:600; color:var(--suave); }
.nivel-trivia.on { border-color:var(--azul); background:var(--tarj2); box-shadow:0 0 0 1px var(--azul) inset; }
.opcion-trivia { min-height:54px; padding:12px 16px; border-radius:12px; border:1.5px solid var(--borde); background:var(--tarj); font-size:15px; font-weight:700; text-align:left; }
.opcion-trivia.bien { background:#12452D; border-color:var(--verde); }
.opcion-trivia.mal { background:#4A1A20; border-color:var(--vivo); }
.barra-tiempo { height:6px; border-radius:3px; background:var(--sup); overflow:hidden; }
.barra-tiempo div { height:100%; width:100%; background:var(--azul); transition:width 1s linear; }
.aviso-gol { position:fixed; left:50%; top:10px; z-index:60; width:min(520px, calc(100% - 20px)); transform:translate(-50%, -140%); transition:transform .35s cubic-bezier(.2,1.3,.4,1);
  display:flex; gap:10px; align-items:center; padding:12px; border-radius:18px; background:rgba(34,46,74,.97); border:1px solid #3B5686; box-shadow:0 12px 34px rgba(0,0,0,.55); color:var(--texto); }
.aviso-gol.on { transform:translate(-50%, 0); }
.aviso-gol img { width:42px; height:42px; border-radius:11px; background:var(--fondo); padding:2px; flex:none; }
.aviso-gol small { display:block; font-size:11px; color:#AEB9CC; font-weight:600; }
.aviso-gol b { display:block; font-size:15px; font-weight:900; margin-top:2px; }
.aviso-gol span span, .aviso-gol > span > span { display:block; font-size:12.5px; color:#DCE3EE; }
.camisetas svg.kit-pix { width:96px; height:96px; max-width:100%; display:block; margin:0 auto; }
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
.ficha-j { position:relative; width:48px; height:48px; border-radius:24px; border:3px solid var(--c); background:#0D1626; display:flex; align-items:center; justify-content:center; box-shadow:0 0 0 2px rgba(5,7,13,.6), 0 3px 10px rgba(0,0,0,.45); }
.ficha-j img { width:28px; height:28px; object-fit:contain; }
.ficha-j .pos-txt { font-size:11px; font-weight:900; color:var(--c); letter-spacing:.3px; }
.ficha-j .num { position:absolute; right:-6px; bottom:-4px; min-width:19px; height:19px; padding:0 4px; border-radius:10px; background:var(--c); color:#05070D; font-size:10px; font-weight:900; display:flex; align-items:center; justify-content:center; border:2px solid #0D1626; }
.leyenda-pos { display:flex; flex-wrap:wrap; justify-content:center; gap:6px 14px; padding:0 12px 12px; font-size:11.5px; font-weight:700; color:var(--suave); }
.leyenda-pos i { display:inline-block; width:10px; height:10px; border-radius:5px; margin-right:5px; vertical-align:-1px; }
.suplentes { margin:0 12px 12px; border-radius:14px; background:#0A1220; border:1px solid var(--linea); overflow:hidden; }
.suplentes h3 { margin:0; padding:11px 14px; font-size:13px; letter-spacing:.6px; color:var(--suave); border-bottom:1px solid var(--linea); display:flex; justify-content:space-between; }
.sup { display:flex; align-items:center; gap:12px; padding:10px 14px; border-top:1px solid rgba(255,255,255,.04); }
.sup:first-of-type { border-top:0; }
.sup .num { flex:none; width:32px; height:32px; border-radius:16px; display:flex; align-items:center; justify-content:center; font-weight:900; font-size:13px; background:#16233B; border:2px solid var(--c); color:var(--texto); }
.sup .n { flex:1; min-width:0; font-weight:700; font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sup .sup-pos { font-size:11px; font-weight:800; padding:3px 8px; border-radius:999px; color:var(--c); background:rgba(255,255,255,.05); white-space:nowrap; }
@keyframes titila { 50% { opacity:.25; } }
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
const CON_GOL = ['LPF', 'LIB', 'SUD', 'CAR', 'PNA'];  // torneos de Highlightly con tabla de goleadores
const COLOR = { CL:'#8DB8FF', PL:'#C084FC', PD:'#FB923C', SA:'#2DD4BF', BL1:'#F87171', FL1:'#FACC15', PPL:'#22C55E', DED:'#F97316', ELC:'#A3A3A3', BSA:'#4ADE80', WC:'#E879F9', LPF:'#60A5FA', FEDA:'#A5B4FC', PRC:'#94A3B8', PRD:'#64748B', TDC:'#F5C400', SCA:'#E879F9', CAR:'#F472B6', LIB:'#FBBF24', SUD:'#34D399', PNA:'#93C5FD', URU:'#7DD3FC', CHI:'#EF4444', COL:'#FDE047', PAR:'#F87171', PER:'#FB7185', ECU:'#FACC15', BOL:'#4ADE80', VEN:'#B45309', MLS:'#60A5FA', LMX:'#22C55E', SAU:'#10B981', UEL:'#F59E0B', UECL:'#34D399', FAC:'#E5E7EB', CDR:'#F87171', CIT:'#38BDF8', DFB:'#FCA5A5', CDF:'#93C5FD', CAM:'#FBBF24', EURO:'#818CF8' };
const DIAS = ['Dom','Lun','Mar','Mié','Jue','Vie','Sáb'];
const DIAS_L = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
const MESES = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
const MESES_L = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
// Si la pantalla ya cambió mientras se cargaban datos, devuelve un elemento suelto (no rompe nada)
const $ = (id) => document.getElementById(id) || document.createElement('div');
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

// Pide datos al servidor. Si el servidor se está despertando o falla un momento, reintenta solo (hasta 4 veces).
// Memoria del celular: lo que ya se vio se muestra al instante y se actualiza por detrás.
// Segundos que cada dato se considera "fresco" (los partidos de hoy, poco; el resto, más).
const FRESCO = [[/^\/api\/calendario_mes/, 120], [/^\/api\/hoy/, 20], [/^\/api\/partido/, 20], [/^\/api\/estadisticas/, 60], [/^\/api\/calendario/, 900], [/^\/api\/tabla/, 300],
  [/^\/api\/goleadores/, 1800], [/^\/api\/noticias/, 300], [/^\/api\/equipos/, 86400], [/^\/api\/club/, 86400], [/^\/api\/proximos/, 900], [/^\/api\/jugador_extra/, 86400]];
const VIVOS = /^\/api\/(hoy|partido|estadisticas)/;   // estos nunca se muestran viejos
const memoria = {};
function memLeer(ruta) { if (memoria[ruta]) return memoria[ruta]; try { const x = JSON.parse(sessionStorage.getItem('m:' + ruta)); if (x) memoria[ruta] = x; return x; } catch (e) { return null; } }
function memGuardar(ruta, d) { const x = { t: Date.now(), d }; memoria[ruta] = x; try { sessionStorage.setItem('m:' + ruta, JSON.stringify(x)); } catch (e) {} }
async function pedir(ruta, intento = 0) {
  const regla = FRESCO.find(([re]) => re.test(ruta));
  if (regla && intento === 0) {
    const m = memLeer(ruta);
    if (m && Date.now() - m.t < regla[1] * 1000) return m.d;
    if (m && !VIVOS.test(ruta)) { pedirDeVerdad(ruta).catch(() => {}); return m.d; }  // viejo: se muestra y se actualiza por detrás
  }
  return pedirDeVerdad(ruta, intento);
}
async function pedirDeVerdad(ruta, intento = 0) {
  let r, d;
  try {
    r = await fetch(ruta);
    d = await r.json();
  } catch (e) {
    if (intento < 4) { await new Promise(ok => setTimeout(ok, 1500 * (intento + 1))); return pedirDeVerdad(ruta, intento + 1); }
    throw new Error('No hay conexión con Tiki Data. Probá de nuevo en un ratito.');
  }
  if (!r.ok) {
    if (r.status >= 500 && intento < 2) { await new Promise(ok => setTimeout(ok, 2000)); return pedirDeVerdad(ruta, intento + 1); }
    throw new Error(d.error || 'Error desconocido');
  }
  if (FRESCO.some(([re]) => re.test(ruta)) && !d.calculando && !d.preparando && !(/^\/api\/noticias/.test(ruta) && !(d.noticias || []).length && !(d.destacadas || []).length) && !(d.faltan && d.faltan.length)) memGuardar(ruta, d);
  return d;
}
function escImg(t) { const u = t && (t.escudo || (typeof ESC_CACHE !== 'undefined' && ESC_CACHE[t.nombre])); return u ? `<img src="${esc(u)}" alt="" loading="lazy">` : ''; }
function escudo(t, cls='esc') {
  const club = t && t.id ? ` data-club="${t.id}" role="link" tabindex="0" aria-label="Ver club ${esc(t.nombre)}"` : '';
  return t && t.escudo ? `<img class="${cls}" src="${esc(t.escudo)}" alt="" loading="lazy"${club}>`
    : (t && t.nombre && ESC_CACHE[t.nombre] ? `<img class="${cls}" src="${esc(ESC_CACHE[t.nombre])}" alt="" loading="lazy"${club}>`
    : `<span class="${cls} ph"${club}${t && t.nombre && ESC_CACHE[t.nombre] === undefined ? ` data-buscar="${esc(t.nombre)}"` : ''}>${esc(((t && (t.sigla || t.nombre)) || '?').slice(0,3))}</span>`);
}
// Escudos que faltan: se buscan de a varios y se guardan en el celular
let ESC_CACHE = {};
try { ESC_CACHE = JSON.parse(localStorage.getItem('tiki_escudos') || '{}'); } catch (e) {}
let buscandoEscudos = false;
function completarEscudos() {
  if (buscandoEscudos) return;
  const faltan = [...new Set([...document.querySelectorAll('.ph[data-buscar]')].map(e => e.dataset.buscar))].filter(n => ESC_CACHE[n] === undefined).slice(0, 20);
  if (!faltan.length) return;
  buscandoEscudos = true;
  fetch('/api/escudos?nombres=' + encodeURIComponent(faltan.join('|'))).then(r => r.ok ? r.json() : {}).then(d => {
    faltan.forEach(n => { ESC_CACHE[n] = (d && d[n]) || ''; });
    try { localStorage.setItem('tiki_escudos', JSON.stringify(ESC_CACHE)); } catch (e) {}
    document.querySelectorAll('.ph[data-buscar]').forEach(el => {
      const url = ESC_CACHE[el.dataset.buscar];
      if (url) { const img = document.createElement('img'); img.className = el.className.replace(' ph', ''); img.src = url; img.alt = ''; img.loading = 'lazy';
        if (el.dataset.club) { img.dataset.club = el.dataset.club; img.setAttribute('role', 'link'); img.tabIndex = 0; } el.replaceWith(img); }
      else if (url === '') el.removeAttribute('data-buscar');
    });
  }).catch(() => {}).finally(() => { buscandoEscudos = false; setTimeout(completarEscudos, 300); });
}
new MutationObserver(() => { clearTimeout(completarEscudos.t); completarEscudos.t = setTimeout(completarEscudos, 400); }).observe(document.body, { childList: true, subtree: true });
// En lugar de las iniciales: la silueta de un jugador
function iniciales(n) { return '<svg class="silueta" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4.2" fill="currentColor"/><path d="M3.8 21c.6-4.6 4-7.4 8.2-7.4s7.6 2.8 8.2 7.4z" fill="currentColor"/></svg>'; }
// El minuto avanza solo en el celular entre una actualización y la otra
function minutoAhora(min, desde) {
  const m = parseInt(min, 10);
  if (isNaN(m)) return min ? min + "'" : 'Vivo';
  const v = m + Math.max(0, Math.floor((Date.now() - desde) / 60000));
  const tope = m <= 45 ? 45 : m <= 90 ? 90 : m <= 105 ? 105 : 120;
  return v > tope ? tope + "+'" : v + "'";
}
function marcaMinuto(p) { if (!p.vivo || p.estado === 'PAUSED' || !p.minuto) return ''; return ` data-min="${esc(String(p.minuto))}" data-desde="${p.act ? p.act * 1000 : Date.now()}"`; }
setInterval(() => document.querySelectorAll('[data-min]').forEach(el => { const t = minutoAhora(el.dataset.min, +el.dataset.desde); if (el.firstChild && el.firstChild.nodeType === 3) el.firstChild.nodeValue = t; else if (!el.children.length) el.textContent = t; }), 15000);
function estadoTexto(p) {
  if (p.vivo) return p.estado === 'PAUSED' ? 'ET' : (p.minuto ? minutoAhora(p.minuto, p.act ? p.act * 1000 : Date.now()) : 'Vivo');
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
    <span class="st ${p.vivo ? 'vivo' : ''}"${mostrarLiga ? '' : marcaMinuto(p)}>${st}</span>
    <span class="fila-c"><span class="fila-top">
      <span class="eq der ${pl ? 'pierde' : ''}"><span class="nom">${esc(p.local.nombre)}</span>${escudo(p.local)}</span>
      <span class="marc ${p.vivo ? 'vivo' : jugado ? 'jug' : ''}">${jugado ? p.gl + ' - ' + p.gv : 'vs'}</span>
      <span class="eq ${pv ? 'pierde' : ''}">${escudo(p.visitante)}<span class="nom">${esc(p.visitante.nombre)}</span></span></span>
    ${jugado && (p.terminado || p.vivo) ? `<span class="fila-goles" data-goles="${p.id}"${p.vivo ? ' data-vivo="1"' : ''}>${RES_GOLES[p.id] ? htmlGolesFila(RES_GOLES[p.id].d) : ''}</span>` : ''}</span>
  </a>`;
}
// Goleadores y rojas debajo de cada partido de la lista (sin tener que entrar al partido)
const RES_GOLES = {};
function htmlGolesFila(r) {
  if (!r) return '';
  const lado = (l) => r.g.filter(g => g[2] === l).map(g => `<span><em>${esc(g[0])}'</em> ${esc(String(g[1]).split(' ').slice(-1)[0])}${g[3] === 'PENALTY' ? ' (p)' : g[3] === 'OWN' ? ' (e.c.)' : ''}</span>`).join('');
  const roja = (n) => n ? `<span class="fg-roja">${'<i></i>'.repeat(n)}</span>` : '';
  if (!r.g.length && !r.rL && !r.rV) return '';
  if (!r.g.length) return '';
  return `<span class="fg izq">${lado('L')}</span><span class="fg">${lado('V')}</span>`;
}
function ponerRojas(id, r) {
  const a = document.querySelector(`a.fila[href="#/partido/${id}"]`); if (!a || !r) return;
  const eqs = a.querySelectorAll('.eq');
  [[eqs[0], r.rL], [eqs[1], r.rV]].forEach(([el, n]) => { if (el && n && !el.querySelector('.fg-roja')) el.insertAdjacentHTML(el === eqs[0] ? 'beforeend' : 'afterbegin', `<span class="fg-roja">${'<i></i>'.repeat(n)}</span>`); });
}
let buscandoGoles = false;
function completarGolesFilas() {
  if (buscandoGoles) return;
  const ahora = Date.now();
  const els = [...document.querySelectorAll('[data-goles]')];
  els.forEach(el => { const x = RES_GOLES[el.dataset.goles]; if (x) ponerRojas(el.dataset.goles, x.d); });
  const faltan = [...new Set(els.map(e => e.dataset.goles))].filter(id => { const x = RES_GOLES[id]; return !x || (x.vivo && ahora - x.t > 300000); }).slice(0, 30);
  if (!faltan.length) return;
  buscandoGoles = true;
  fetch('/api/resumen_goles?ids=' + faltan.join(',')).then(r => r.ok ? r.json() : {}).then(d => {
    faltan.forEach(id => { const vivo = !!document.querySelector(`[data-goles="${id}"][data-vivo]`); RES_GOLES[id] = { d: d[id] || null, t: Date.now(), vivo }; });
    document.querySelectorAll('[data-goles]').forEach(el => { const x = RES_GOLES[el.dataset.goles]; if (x) { el.innerHTML = htmlGolesFila(x.d); ponerRojas(el.dataset.goles, x.d); } });
  }).catch(() => {}).finally(() => { buscandoGoles = false; });
}
new MutationObserver(() => { clearTimeout(completarGolesFilas.t); completarGolesFilas.t = setTimeout(completarGolesFilas, 350); }).observe(document.body, { childList: true, subtree: true });
function error(e) { return `<div class="tarjeta aviso">No se pudo cargar.<br>${esc(e.message)}</div>`; }
function volver(titulo, destino='#/') {
  return `<div class="volver"><a class="icono" href="${destino}" aria-label="Volver">${svg(ICON.atras)}</a><b>${esc(titulo)}</b></div>`;
}

/* ---------- Menú y navegación ---------- */
// Mientras algo carga: datos curiosos que van cambiando
const DATOS_CARGA = [
  'El gol más rápido de los Mundiales lo hizo Hakan Şükür (Turquía) en 2002: a los 11 segundos.',
  'Messi es el jugador con más partidos jugados en Mundiales: 26.',
  'Las tarjetas amarilla y roja se usaron por primera vez en el Mundial de 1970.',
  'El VAR se usó por primera vez en un Mundial en Rusia 2018.',
  'El primer partido entre selecciones fue Escocia vs Inglaterra, en 1872. Terminó 0 a 0.',
  'Lev Yashin es el único arquero que ganó el Balón de Oro (1963).',
  'Martín Palermo erró tres penales en un mismo partido: contra Colombia, en la Copa América 1999.',
  'Solo Italia (1934 y 1938) y Brasil (1958 y 1962) ganaron dos Mundiales seguidos.',
  'La primera Copa Libertadores fue en 1960 y la ganó Peñarol.',
  'Batistuta hizo tres goles en un partido en dos Mundiales distintos: 1994 y 1998.',
  'Maradona jugó 4 Mundiales: 1982, 1986, 1990 y 1994.',
  'El penal existe desde 1891.',
  'Si un equipo se queda con menos de 7 jugadores, el partido se suspende.',
  'Cristiano Ronaldo es el máximo goleador de la historia de la Champions.',
  'El Mundial 2026 fue el primero con 48 selecciones.',
  'En la final del Mundial 1950, en el Maracaná, había más de 170.000 personas.'
];
let datoN = Math.floor(Math.random() * DATOS_CARGA.length);
function ponerDato(el) {
  if (!el.querySelector('.anim-gol')) el.insertAdjacentHTML('afterbegin', '<div class="anim-gol" aria-hidden="true"><span class="piso"></span><span class="arco"></span><span class="bola"></span><b class="txt-gol">¡GOL!</b></div>');
  let d = el.querySelector('.dato-carga');
  if (!d) { d = document.createElement('span'); d.className = 'dato-carga'; el.appendChild(d); }
  datoN = (datoN + 1) % DATOS_CARGA.length;
  d.innerHTML = '<b>¿SABÍAS QUE…?</b>' + esc(DATOS_CARGA[datoN]);
  d.style.animation = 'none'; void d.offsetWidth; d.style.animation = '';
}
setInterval(() => document.querySelectorAll('.cargando').forEach(ponerDato), 3500);
new MutationObserver(() => document.querySelectorAll('.cargando:not([data-dato])').forEach(el => {
  el.dataset.dato = '1';
  el.insertAdjacentHTML('afterbegin', '<div class="anim-gol" aria-hidden="true"><span class="piso"></span><span class="arco"></span><span class="bola"></span><b class="txt-gol">¡GOL!</b></div>');
  setTimeout(() => { if (el.isConnected) ponerDato(el); }, 900);
})).observe(document.body, { childList: true, subtree: true });
function armarMenu() {
  const grupos = [['FÚTBOL ARGENTINO', ['LPF','CAR','TDC','SCA','PNA','BMETRO','FEDA','PRC','PRD']], ['COPAS SUDAMERICANAS', ['LIB','SUD']],
    ['TORNEOS DE EUROPA', ['CL','UEL','UECL']], ['LAS 5 GRANDES LIGAS', ['PL','PD','BL1','SA','FL1']],
    ['MÁS LIGAS', ['BSA','PPL','DED','ELC','MLS','LMX','URU','CHI','COL','PAR','PER','ECU','BOL','VEN','SAU']],
    ['COPAS NACIONALES', ['FAC','CDR','CIT','DFB','CDF']], ['SELECCIONES', ['WC','CAM','EURO']]]
    .map(([n, cods]) => [n, cods.filter(c => LIGAS[c] || c === 'BMETRO')]).filter(g => g[1].some(c => LIGAS[c]));
  let h = `<div class="grupo">
    <a class="item" href="#/">${svg(ICON.inicio,20)}Inicio</a>
    <a class="item" href="#/calendario">${svg(ICON.cal,20)}Calendario</a>
    <a class="item" href="#/favoritos">${svg(ICON.estrella,20)}Mis favoritos</a>
    <a class="item" href="#/favoritos"><span style="width:20px;text-align:center">🔔</span>Avisos de gol en vivo</a>
    <a class="item" href="#/envivo"><span style="width:20px;display:flex;justify-content:center"><span class="punto" style="background:#FF3B3B;width:10px;height:10px"></span></span>En vivo gratis</a>
    <a class="item" href="#/trivia"><span style="width:20px;text-align:center">🧠</span>Trivia de fútbol</a>
    <button class="item" id="btnInstalar" style="border:0;background:var(--azulreal);width:100%;margin-top:6px" ${puedeInstalar() ? '' : 'hidden'}>${svg('M12 3v12M7 10l5 5 5-5M5 21h14',20)}Instalar la app en el celular</button></div>`;
  grupos.forEach(([n, cods]) => {
    h += `<div class="grupo"><small>${n}</small>` + cods.map(c => c === 'BMETRO'
      ? `<span class="item proximo"><span class="punto" style="background:#5B6578"></span>Primera B Metro<em class="tag-prox">Próximamente</em></span>`
      : `<a class="item" href="#/liga/${c}"><span class="punto" style="background:${COLOR[c] || '#60A5FA'}"></span>${esc(LIGAS[c][0])}</a>`).join('');
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
             ['favoritos','#/favoritos','Favoritos',ICON.estrella]];
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
  pintarHoy(fecha, false);
}
let timerHoy = null; const MARC_PREV = {};
async function pintarHoy(fecha, deNuevo) {
  clearTimeout(timerHoy);
  const favs = leer('tiki_favs', []);
  const hashAntes = location.hash;
  try {
    const d = deNuevo ? await pedirDeVerdad('/api/hoy?fecha=' + fecha) : await pedir('/api/hoy?fecha=' + fecha);
    if (location.hash !== hashAntes || !$('ligasHoy')) return;  // ya se fue a otra pantalla
    // mientras haya partidos jugándose (o por empezar en la próxima hora), se vuelve a pedir cada 20 segundos
    const ahoraMin = new Date().getHours() * 60 + new Date().getMinutes();
    const porEmpezar = (p) => !p.terminado && !p.vivo && p.hora && /^\d\d:\d\d$/.test(p.hora) && (() => { const m = +p.hora.slice(0,2) * 60 + +p.hora.slice(3); return m - ahoraMin <= 60 && ahoraMin - m <= 150; })();
    const activos = fecha === hoyISO() && d.grupos.some(g => g.partidos.some(p => p.vivo || porEmpezar(p)));
    if (activos) timerHoy = setTimeout(() => { if (location.hash === hashAntes && document.visibilityState === 'visible') pintarHoy(fecha, true); else if (location.hash === hashAntes) document.addEventListener('visibilitychange', function f() { if (document.visibilityState === 'visible') { document.removeEventListener('visibilitychange', f); if (location.hash === hashAntes) pintarHoy(fecha, true); } }); }, 20000);
    // si cambió el resultado, se vuelven a buscar los goleadores de ese partido
    d.grupos.forEach(g => g.partidos.forEach(p => { const k = p.gl + '-' + p.gv; if (MARC_PREV[p.id] && MARC_PREV[p.id] !== k) delete RES_GOLES[p.id]; MARC_PREV[p.id] = k; }));
    const todos = d.grupos.flatMap(g => g.partidos);
    const vivos = todos.filter(p => p.vivo);
    if (vivos.length) $('vivos').innerHTML = `<div class="vivos"><span class="tag">EN VIVO</span>` + vivos.map(p => `<a class="chipvivo" href="#/partido/${p.id}"><b${marcaMinuto(p)}>${esc(estadoTexto(p))}</b>${escudo(p.local)}<strong>${p.gl ?? 0}-${p.gv ?? 0}</strong>${escudo(p.visitante)}</a>`).join('') + '</div>';
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
    // las ligas más importantes primero (el orden lo arma el servidor, y se repite acá por las dudas)
    const grupos = d.grupos.slice().sort((x, y) => { const ix = ORDEN_LIGAS.indexOf(x.liga), iy = ORDEN_LIGAS.indexOf(y.liga); return (ix < 0 ? 99 : ix) - (iy < 0 ? 99 : iy); });
    const tarjeta = (g) => `<section class="tarjeta">
        <a class="cab" href="#/liga/${g.liga}">${g.emblema ? `<img class="emb" src="${esc(g.emblema)}" alt="">` : ''}<div class="t"><b>${esc(g.nombre)}</b><small>${esc(g.region)}</small></div>${svg(ICON.sig,16)}</a>
        ${g.partidos.map(p => filaPartido(p)).join('')}</section>`;
    $('ligasHoy').innerHTML = grupos.length ? grupos.map(tarjeta).join('')
      : `<div class="tarjeta aviso">No hay partidos de las ligas disponibles este día.<br><a class="link" href="#/calendario">Ver el calendario</a></div>`;
  } catch (e) { if (!deNuevo && $('ligasHoy')) $('ligasHoy').innerHTML = error(e); else if (deNuevo) timerHoy = setTimeout(() => { if (location.hash === hashAntes) pintarHoy(fecha, true); }, 20000); }
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
    const ruta = '/api/noticias?prefs=' + encodeURIComponent(prefs.join(','));
    let d = await pedir(ruta);
    if (d.preparando) {
      el.innerHTML = '<div class="cargando">Buscando las últimas noticias…</div>';
      for (let i = 0; i < 20 && d.preparando; i++) { await new Promise(ok => setTimeout(ok, 4000)); if (!el.isConnected) return; d = await pedirDeVerdad(ruta); }
      if (d.preparando) { el.innerHTML = '<div class="tarjeta aviso">Las noticias están tardando. Probá de nuevo en un ratito.</div>'; return; }
      memGuardar(ruta, d);
    }
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

/* ---------- Trivia de fútbol (estilo Preguntados) ---------- */
// Cada pregunta: [categoría, pregunta, respuesta correcta, otras 3 opciones, nivel (1 fácil, 2 medio, 3 difícil)]
const PREGUNTAS = [
  ['sel', '¿Cuántos Mundiales ganó la Selección Argentina?', '3', ['2', '4', '1'], 1],
  ['sel', '¿En qué país se jugó el Mundial 2022?', 'Qatar', ['Rusia', 'Brasil', 'Arabia Saudita'], 1],
  ['sel', '¿Quién era el DT de Argentina en el Mundial 2022?', 'Lionel Scaloni', ['Jorge Sampaoli', 'Gerardo Martino', 'Alejandro Sabella'], 1],
  ['sel', '¿Contra qué selección jugó Argentina la final del Mundial 2022?', 'Francia', ['Croacia', 'Brasil', 'Países Bajos'], 1],
  ['sel', '¿Quién atajó en la final del Mundial 2022 para Argentina?', 'Emiliano "Dibu" Martínez', ['Franco Armani', 'Gerónimo Rulli', 'Sergio Romero'], 1],
  ['sel', '¿Quién era el DT de Argentina campeón del mundo en 1986?', 'Carlos Bilardo', ['César Luis Menotti', 'Alfio Basile', 'Marcelo Bielsa'], 2],
  ['sel', '¿Quién era el DT de Argentina campeón del mundo en 1978?', 'César Luis Menotti', ['Carlos Bilardo', 'Juan Carlos Lorenzo', 'Daniel Passarella'], 2],
  ['sel', '¿Qué selección ganó más Mundiales en la historia?', 'Brasil', ['Alemania', 'Italia', 'Argentina'], 1],
  ['sel', '¿Contra quién le hizo Maradona el famoso "Gol del Siglo" en 1986?', 'Inglaterra', ['Bélgica', 'Alemania', 'Italia'], 2],
  ['sel', '¿Quién hizo el gol de Argentina en la final de la Copa América 2021?', 'Ángel Di María', ['Lionel Messi', 'Lautaro Martínez', 'Rodrigo De Paul'], 2],
  ['sel', '¿En qué estadio Argentina le ganó a Brasil la final de la Copa América 2021?', 'Maracaná', ['Morumbí', 'Mineirão', 'Monumental'], 2],
  ['sel', '¿Quién hizo el gol de Argentina en la final de la Copa América 2024?', 'Lautaro Martínez', ['Julián Álvarez', 'Lionel Messi', 'Ángel Di María'], 2],
  ['sel', '¿Contra qué selección ganó Argentina la final de la Copa América 2024?', 'Colombia', ['Uruguay', 'Brasil', 'Canadá'], 2],
  ['sel', '¿Quién es el máximo goleador de la historia de la Selección Argentina?', 'Lionel Messi', ['Gabriel Batistuta', 'Sergio Agüero', 'Diego Maradona'], 1],
  ['sel', '¿Qué tres países organizan el Mundial 2026?', 'Estados Unidos, México y Canadá', ['Brasil, Argentina y Uruguay', 'España, Portugal y Marruecos', 'Estados Unidos, México y Costa Rica'], 1],
  ['sel', '¿Cuántas selecciones juegan el Mundial 2026?', '48', ['32', '40', '64'], 2],
  ['sel', '¿Qué país ganó el primer Mundial, en 1930?', 'Uruguay', ['Argentina', 'Italia', 'Brasil'], 3],
  ['sel', '¿Quién fue el goleador del Mundial 1978?', 'Mario Kempes', ['Daniel Passarella', 'Leopoldo Luque', 'René Houseman'], 3],
  ['sel', '¿Quién hizo el gol de Alemania en la final del Mundial 2014 contra Argentina?', 'Mario Götze', ['Thomas Müller', 'Miroslav Klose', 'Toni Kroos'], 3],
  ['sel', '¿En qué país se jugó el Mundial 2014?', 'Brasil', ['Sudáfrica', 'Alemania', 'Rusia'], 2],
  ['arg', '¿Qué club tiene más Copas Libertadores en la historia?', 'Independiente', ['Boca Juniors', 'River Plate', 'Peñarol'], 2],
  ['arg', '¿De qué club es el estadio apodado "La Bombonera"?', 'Boca Juniors', ['River Plate', 'Racing', 'Huracán'], 1],
  ['arg', '¿Cómo se llama el partido entre Boca y River?', 'Superclásico', ['Clásico de Avellaneda', 'Derbi porteño', 'Clásico rosarino'], 1],
  ['arg', '¿Qué equipos juegan el Clásico de Avellaneda?', 'Racing e Independiente', ['Lanús y Banfield', 'Boca y River', 'Arsenal y Racing'], 1],
  ['arg', '¿A qué club le dicen "El Ciclón"?', 'San Lorenzo', ['Huracán', 'Vélez', 'Tigre'], 2],
  ['arg', '¿En qué año ganó San Lorenzo su primera Copa Libertadores?', '2014', ['2008', '2016', '2002'], 3],
  ['arg', '¿En qué ciudad se jugó la final de la Libertadores 2018 entre River y Boca?', 'Madrid', ['Buenos Aires', 'Barcelona', 'Asunción'], 2],
  ['arg', '¿Qué club argentino ganó la Copa Libertadores 2009?', 'Estudiantes de La Plata', ['Boca Juniors', 'Vélez', 'Lanús'], 3],
  ['arg', '¿Qué equipos juegan el clásico rosarino?', 'Rosario Central y Newell\'s', ['Central Córdoba y Newell\'s', 'Rosario Central y Unión', 'Newell\'s y Colón'], 2],
  ['arg', '¿A qué club le dicen "Pincharrata"?', 'Estudiantes de La Plata', ['Gimnasia', 'Platense', 'Banfield'], 2],
  ['arg', '¿Qué club argentino ganó la Copa Sudamericana 2024?', 'Racing', ['Lanús', 'Boca Juniors', 'Defensa y Justicia'], 2],
  ['arg', '¿A qué club le dicen "La Academia"?', 'Racing', ['Estudiantes', 'River Plate', 'Argentinos Juniors'], 2],
  ['arg', '¿Contra qué club ganó Vélez la Copa Intercontinental de 1994?', 'Milan', ['Real Madrid', 'Juventus', 'Ajax'], 3],
  ['arg', '¿Contra qué club ganó Boca la Copa Intercontinental del año 2000?', 'Real Madrid', ['Milan', 'Bayern Múnich', 'Barcelona'], 3],
  ['arg', '¿En qué barrio está La Bombonera?', 'La Boca', ['Núñez', 'Palermo', 'Barracas'], 1],
  ['arg', '¿De qué club es el estadio "Más Monumental"?', 'River Plate', ['Boca Juniors', 'Vélez', 'San Lorenzo'], 1],
  ['arg', '¿A qué club le dicen "El Calamar"?', 'Platense', ['Aldosivi', 'Tigre', 'Sarmiento'], 2],
  ['arg', '¿A qué club le dicen "El Tiburón"?', 'Aldosivi', ['Talleres', 'Belgrano', 'Tigre'], 3],
  ['arg', '¿A qué club le dicen "El Globo"?', 'Huracán', ['San Lorenzo', 'Platense', 'Lanús'], 2],
  ['arg', '¿A qué club le dicen "El Fortín"?', 'Vélez', ['Ferro', 'All Boys', 'Atlanta'], 2],
  ['est', '¿Cuántos Balones de Oro ganó Lionel Messi?', '8', ['7', '6', '5'], 2],
  ['est', '¿En qué club de Rosario jugó Messi de chico?', 'Newell\'s', ['Rosario Central', 'Central Córdoba', 'Argentino de Rosario'], 2],
  ['est', '¿En qué club europeo debutó Messi como profesional?', 'Barcelona', ['PSG', 'Real Madrid', 'Atlético de Madrid'], 1],
  ['est', '¿Quién es el máximo goleador de la historia de la Champions League?', 'Cristiano Ronaldo', ['Lionel Messi', 'Robert Lewandowski', 'Raúl'], 2],
  ['est', '¿En qué club de Italia fue ídolo Diego Maradona?', 'Napoli', ['Juventus', 'Roma', 'Milan'], 2],
  ['est', '¿De qué país es Erling Haaland?', 'Noruega', ['Dinamarca', 'Suecia', 'Alemania'], 2],
  ['est', '¿En qué club brasileño fue ídolo Pelé?', 'Santos', ['Flamengo', 'Corinthians', 'Palmeiras'], 3],
  ['est', '¿Cuál es el apodo de Cristiano Ronaldo?', 'CR7', ['El Bicho', 'La Pulga', 'El Fenómeno'], 1],
  ['est', '¿Cuál es el apodo de Lionel Messi?', 'La Pulga', ['El Pipita', 'El Kun', 'El Apache'], 1],
  ['est', '¿A quién le dicen "El Kun"?', 'Sergio Agüero', ['Carlos Tevez', 'Gonzalo Higuaín', 'Paulo Dybala'], 1],
  ['est', '¿A quién le dicen "La Araña"?', 'Julián Álvarez', ['Lautaro Martínez', 'Enzo Fernández', 'Alexis Mac Allister'], 2],
  ['est', '¿De qué país es Kylian Mbappé?', 'Francia', ['Bélgica', 'Camerún', 'Portugal'], 1],
  ['est', '¿En qué club de Estados Unidos juega Messi desde 2023?', 'Inter Miami', ['LA Galaxy', 'New York City', 'Orlando City'], 1],
  ['eur', '¿Qué club ganó más veces la Champions League?', 'Real Madrid', ['Milan', 'Barcelona', 'Bayern Múnich'], 1],
  ['eur', '¿Cómo se llama el estadio del Real Madrid?', 'Santiago Bernabéu', ['Camp Nou', 'Metropolitano', 'Mestalla'], 1],
  ['eur', '¿Cómo se llama el estadio del Barcelona?', 'Camp Nou', ['Santiago Bernabéu', 'San Mamés', 'Riazor'], 1],
  ['eur', '¿Qué club inglés tiene el apodo de "Los Diablos Rojos"?', 'Manchester United', ['Liverpool', 'Arsenal', 'Nottingham Forest'], 2],
  ['eur', '¿Cómo se llama la liga de primera división de Alemania?', 'Bundesliga', ['Serie A', 'Eredivisie', 'Ligue 1'], 2],
  ['eur', '¿Qué equipos juegan el "Derbi della Madonnina"?', 'Inter y Milan', ['Roma y Lazio', 'Juventus y Torino', 'Napoli y Roma'], 3],
  ['eur', '¿Cómo se llama el estadio del Liverpool?', 'Anfield', ['Old Trafford', 'Goodison Park', 'Emirates'], 2],
  ['eur', '¿En qué ciudad juega la Juventus?', 'Turín', ['Milán', 'Roma', 'Génova'], 2],
  ['eur', '¿En qué año ganó el PSG su primera Champions League?', '2025', ['2020', '2024', '2023'], 3],
  ['eur', '¿Cómo se llama el estadio del Manchester United?', 'Old Trafford', ['Anfield', 'Etihad', 'Stamford Bridge'], 2],
  ['eur', '¿Cómo se llama la primera división de Italia?', 'Serie A', ['LaLiga', 'Premier League', 'Primeira Liga'], 1],
  ['eur', '¿En qué país juega el Ajax?', 'Países Bajos', ['Bélgica', 'Dinamarca', 'Suiza'], 2],
  ['reg', '¿Cuántos jugadores tiene cada equipo en la cancha?', '11', ['10', '12', '9'], 1],
  ['reg', '¿Cuántos minutos dura un partido sin contar el descuento?', '90', ['80', '100', '120'], 1],
  ['reg', '¿Qué tarjeta significa expulsión?', 'Roja', ['Amarilla', 'Verde', 'Azul'], 1],
  ['reg', '¿Desde cuántos metros del arco se patea un penal?', '11 metros', ['9 metros', '12 metros', '16 metros'], 2],
  ['reg', '¿Cuántos cambios puede hacer cada equipo en un partido oficial?', '5', ['3', '4', '6'], 2],
  ['reg', '¿Qué significa VAR?', 'Árbitro asistente de video', ['Video de alta resolución', 'Verificación automática de reglas', 'Velocidad y alcance del remate'], 2],
  ['reg', '¿Cuánto dura cada tiempo suplementario?', '15 minutos', ['10 minutos', '20 minutos', '30 minutos'], 3],
  ['reg', '¿Cuántas tarjetas amarillas en un mismo partido equivalen a una roja?', '2', ['3', '1', '4'], 1],
  ['sel', '¿Quién hizo el gol del 3 a 2 de Argentina en la final del Mundial 1986?', 'Jorge Burruchaga', ['Jorge Valdano', 'José Luis Brown', 'Diego Maradona'], 3],
  ['sel', '¿En qué año ganó Argentina su primer Mundial?', '1978', ['1986', '1930', '1966'], 1],
  ['arg', '¿Qué club fue el primer argentino en ganar la Copa Libertadores, en 1964?', 'Independiente', ['Racing', 'Estudiantes de La Plata', 'Boca Juniors'], 3],
  ['eur', '¿Qué club ganó la Champions 2005 con la famosa remontada de Estambul?', 'Liverpool', ['Milan', 'Chelsea', 'Barcelona'], 3],
  ['est', '¿Quién ganó el Balón de Oro 2018, cortando la racha de Messi y Cristiano?', 'Luka Modrić', ['Antoine Griezmann', 'Mohamed Salah', 'Kylian Mbappé'], 3],
  ['reg', '¿Cuánto mide de ancho un arco reglamentario?', '7,32 metros', ['6 metros', '8,50 metros', '7 metros'], 3],
  ['reg', '¿A qué distancia tienen que pararse los rivales en un tiro libre?', '9,15 metros', ['7 metros', '11 metros', '5 metros'], 3],  ['sel', '¿Qué número de camiseta usa Messi en la Selección?', '10', ['19', '30', '9'], 1],
  ['sel', '¿Quién fue el DT de Argentina en el Mundial 2014?', 'Alejandro Sabella', ['Diego Maradona', 'Gerardo Martino', 'Jorge Sampaoli'], 2],
  ['arg', '¿De qué ciudad son Rosario Central y Newell\'s?', 'Rosario', ['Córdoba', 'Santa Fe', 'Mendoza'], 1],
  ['arg', '¿A qué club le dicen "El Lobo"?', 'Gimnasia y Esgrima La Plata', ['Estudiantes', 'Banfield', 'Quilmes'], 2],
  ['arg', '¿A qué club le dicen "El Taladro"?', 'Banfield', ['Lanús', 'Talleres', 'Arsenal'], 2],
  ['arg', '¿A qué club le dicen "El Granate"?', 'Lanús', ['Platense', 'Colón', 'Huracán'], 2],
  ['est', '¿De qué país es Cristiano Ronaldo?', 'Portugal', ['España', 'Brasil', 'Italia'], 1],
  ['est', '¿De qué país es Luka Modrić?', 'Croacia', ['Serbia', 'Eslovenia', 'Bosnia'], 2],
  ['eur', '¿En qué país juega el Bayern Múnich?', 'Alemania', ['Austria', 'Suiza', 'Países Bajos'], 1],
  ['eur', '¿En qué país juega el Manchester City?', 'Inglaterra', ['Escocia', 'Irlanda', 'Gales'], 1],
  ['eur', '¿Cómo se llama el estadio del Bayern Múnich?', 'Allianz Arena', ['Signal Iduna Park', 'Olympiastadion', 'Veltins Arena'], 2],
  ['eur', '¿Qué club ganó la Premier League 2015-16, la sorpresa más grande de la historia?', 'Leicester City', ['Tottenham', 'West Ham', 'Everton'], 3],
  ['reg', '¿Qué tarjeta es una advertencia?', 'Amarilla', ['Roja', 'Verde', 'Blanca'], 1],
  ['reg', '¿Cuánto dura como máximo el entretiempo?', '15 minutos', ['10 minutos', '20 minutos', '5 minutos'], 2],
  ['est', '¿Cuántas Champions League ganó Cristiano Ronaldo?', '5', ['4', '6', '3'], 3],
  ['est', '¿Cuántas Champions League ganó Messi con el Barcelona?', '4', ['3', '5', '2'], 3],
  ['est', '¿De qué país es Mohamed Salah?', 'Egipto', ['Marruecos', 'Argelia', 'Túnez'], 3],
  ['est', '¿Quién ganó el Balón de Oro 2022?', 'Karim Benzema', ['Lionel Messi', 'Robert Lewandowski', 'Kevin De Bruyne'], 3],
  ['est', '¿En qué club inglés juega Erling Haaland desde 2022?', 'Manchester City', ['Manchester United', 'Liverpool', 'Chelsea'], 3],
  ['reg', '¿Qué se cobra si la pelota entra directo al arco desde un saque de banda?', 'Saque de arco', ['Gol', 'Córner', 'Se repite el saque'], 3],
  ['reg', '¿Hay offside si un jugador recibe directo de un saque de banda?', 'No', ['Sí', 'Solo en el área', 'Solo en el segundo tiempo'], 3],
  ['reg', '¿Vale un gol hecho directo de saque de arco en el arco rival?', 'Sí', ['No', 'Solo si lo toca otro jugador', 'Solo en alargue'], 3],
  ['reg', '¿Cuántos árbitros asistentes (de línea) hay en un partido?', '2', ['1', '3', '4'], 2],
  ['sel', '¿Contra qué selección perdió Argentina la final del Mundial 1990?', 'Alemania Federal', ['Italia', 'Brasil', 'Países Bajos'], 3],
  ['sel', '¿A qué selección le ganó Argentina por penales en los cuartos de final de Qatar 2022?', 'Países Bajos', ['Croacia', 'Australia', 'Polonia'], 3],
  ['sel', '¿Cómo terminó la final del Mundial 2022 antes de los penales?', '3 a 3', ['2 a 2', '1 a 1', '3 a 2'], 3],
  ['eur', '¿Qué club ganó la Champions 2010 con José Mourinho como DT?', 'Inter', ['Chelsea', 'Real Madrid', 'Porto'], 3],
  ['eur', '¿Qué club inglés ganó más Champions League?', 'Liverpool', ['Manchester United', 'Chelsea', 'Arsenal'], 3],
  ['eur', '¿Contra qué club perdió el Borussia Dortmund la final de la Champions 2013?', 'Bayern Múnich', ['Real Madrid', 'Barcelona', 'Juventus'], 3],
  // ---------- Nivel IMPOSIBLE (para los que saben todo) ----------
  ['sel', '¿Quién fue el goleador del Mundial 1930?', 'Guillermo Stábile', ['Pedro Cea', 'Héctor Scarone', 'Carlos Peucelle'], 4],
  ['sel', '¿Quién hizo el gol del triunfo de Arabia Saudita ante Argentina en Qatar 2022?', 'Salem Al-Dawsari', ['Saleh Al-Shehri', 'Firas Al-Buraikan', 'Mohammed Al-Owais'], 4],
  ['sel', '¿Cuántos goles hizo Messi en el Mundial 2022?', '7', ['6', '8', '5'], 4],
  ['sel', '¿Quién fue el capitán de Argentina campeón del Mundial 1978?', 'Daniel Passarella', ['Mario Kempes', 'Ubaldo Fillol', 'Américo Gallego'], 4],
  ['sel', '¿Quién pateó el penal del gol de Alemania en la final del Mundial 1990?', 'Andreas Brehme', ['Lothar Matthäus', 'Rudi Völler', 'Jürgen Klinsmann'], 4],
  ['sel', '¿Cómo terminó Argentina vs Serbia y Montenegro en el Mundial 2006?', '6 a 0', ['4 a 0', '3 a 1', '5 a 1'], 4],
  ['sel', '¿Cuántas Copas América ganó Argentina (contando la de 2024)?', '16', ['15', '14', '17'], 4],
  ['sel', '¿Quién fue el árbitro de la final del Mundial 2022?', 'Szymon Marciniak', ['Daniele Orsato', 'Wilton Sampaio', 'Facundo Tello'], 4],
  ['sel', '¿Cuántos partidos de Mundiales jugó Messi hasta Qatar 2022 (récord)?', '26', ['25', '24', '23'], 4],
  ['sel', '¿Quién hizo el 1 a 0 de Argentina, de cabeza, en la final del Mundial 1986?', 'José Luis Brown', ['Jorge Valdano', 'Oscar Ruggeri', 'Héctor Enrique'], 4],
  ['arg', '¿Qué club ganó la primera Copa Libertadores, en 1960?', 'Peñarol', ['Independiente', 'Santos', 'Nacional'], 4],
  ['arg', '¿Quién es el máximo goleador de la era profesional del fútbol argentino?', 'Arsenio Erico', ['Ángel Labruna', 'Martín Palermo', 'Herminio Masantonio'], 4],
  ['arg', '¿Quién es el máximo goleador de la historia de River?', 'Ángel Labruna', ['Bernabé Ferreyra', 'Oscar Más', 'Enzo Francescoli'], 4],
  ['arg', '¿Quién es el máximo goleador de la historia de Boca?', 'Martín Palermo', ['Roberto Cherro', 'Francisco Varallo', 'Domingo Tarasconi'], 4],
  ['arg', '¿Contra qué equipo jugó River la Promoción en la que descendió en 2011?', 'Belgrano', ['Talleres', 'Quilmes', 'Rosario Central'], 4],
  ['arg', '¿Quién hizo el 3 a 1 de River en la final de Madrid 2018 contra Boca?', 'Gonzalo "Pity" Martínez', ['Juan Fernando Quintero', 'Lucas Pratto', 'Rafael Santos Borré'], 4],
  ['arg', '¿Qué club ganó tres Libertadores seguidas entre 1968 y 1970?', 'Estudiantes de La Plata', ['Independiente', 'Racing', 'Boca Juniors'], 4],
  ['arg', '¿Contra qué club ganó Racing la Copa Intercontinental de 1967?', 'Celtic', ['Manchester United', 'Inter', 'Real Madrid'], 4],
  ['arg', '¿Quién es el máximo goleador de la historia de la Copa Libertadores?', 'Alberto Spencer', ['Pedro Rocha', 'Daniel Onega', 'Fernando Morena'], 4],
  ['arg', '¿Qué club argentino ganó la primera Copa Sudamericana, en 2002?', 'San Lorenzo', ['Boca Juniors', 'River Plate', 'Lanús'], 4],
  ['est', '¿Cuántos goles hizo Messi en el año 2012 (récord mundial)?', '91', ['86', '79', '96'], 4],
  ['est', '¿Quién ganó el Balón de Oro 2006?', 'Fabio Cannavaro', ['Gianluigi Buffon', 'Thierry Henry', 'Ronaldinho'], 4],
  ['est', '¿Quién es el único arquero que ganó el Balón de Oro?', 'Lev Yashin', ['Gianluigi Buffon', 'Manuel Neuer', 'Oliver Kahn'], 4],
  ['est', '¿Quién hizo 13 goles en un solo Mundial, en 1958?', 'Just Fontaine', ['Gerd Müller', 'Pelé', 'Sándor Kocsis'], 4],
  ['est', '¿Cuántos goles hizo Miroslav Klose en Mundiales (récord)?', '16', ['15', '14', '17'], 4],
  ['est', '¿En qué club debutó Cristiano Ronaldo como profesional?', 'Sporting de Lisboa', ['Benfica', 'Porto', 'Nacional de Madeira'], 4],
  ['est', '¿Quién ganó el Balón de Oro 2024?', 'Rodri', ['Vinícius Júnior', 'Jude Bellingham', 'Kylian Mbappé'], 4],
  ['est', '¿En qué club argentino debutó Ángel Di María?', 'Rosario Central', ['Newell\'s', 'Talleres', 'Vélez'], 4],
  ['est', '¿Contra qué equipo debutó Messi en partido oficial con el Barcelona, en 2004?', 'Espanyol', ['Albacete', 'Real Madrid', 'Sevilla'], 4],
  ['eur', '¿Qué club ganó la primera Copa de Europa, en 1956?', 'Real Madrid', ['Benfica', 'Milan', 'Stade de Reims'], 4],
  ['eur', '¿Qué club ganó la Champions League 2004?', 'Porto', ['Mónaco', 'Deportivo La Coruña', 'Chelsea'], 4],
  ['eur', '¿Quién hizo el gol del triunfo del Manchester United en la final de la Champions 1999?', 'Ole Gunnar Solskjær', ['Teddy Sheringham', 'David Beckham', 'Dwight Yorke'], 4],
  ['eur', '¿Qué club fue campeón invicto de la Premier League 2003-04?', 'Arsenal', ['Chelsea', 'Manchester United', 'Liverpool'], 4],
  ['eur', '¿Quién es el máximo goleador de la historia de la Premier League?', 'Alan Shearer', ['Wayne Rooney', 'Harry Kane', 'Thierry Henry'], 4],
  ['eur', '¿Qué club ganó más veces la Europa League?', 'Sevilla', ['Liverpool', 'Inter', 'Atlético de Madrid'], 4],
  ['eur', '¿Qué club ganó la Champions 2012 en Múnich, contra el Bayern?', 'Chelsea', ['Borussia Dortmund', 'Barcelona', 'Real Madrid'], 4],
  ['eur', '¿Por cuánto le ganó el PSG al Inter en la final de la Champions 2025?', '5 a 0', ['3 a 0', '4 a 1', '2 a 0'], 4],
  ['reg', '¿Cuánto mide de alto un arco reglamentario?', '2,44 metros', ['2,20 metros', '2,50 metros', '2,30 metros'], 4],
  ['reg', '¿Cuánto mide el radio del círculo central?', '9,15 metros', ['10 metros', '8 metros', '11 metros'], 4],
  ['reg', '¿Cuánto mide la circunferencia de una pelota reglamentaria?', 'Entre 68 y 70 cm', ['Entre 60 y 62 cm', 'Entre 72 y 74 cm', 'Entre 64 y 66 cm'], 4],
  ['reg', '¿Con cuántos jugadores como mínimo puede seguir un partido?', '7', ['6', '8', '9'], 4],
  ['reg', '¿En qué Mundial se usó el VAR por primera vez?', '2018', ['2014', '2022', '2010'], 4],
  ['reg', '¿Desde qué año el arquero no puede agarrar con la mano un pase atrás de un compañero?', '1992', ['1986', '1998', '1990'], 4],
  ['reg', '¿Cuánto pesa una pelota reglamentaria?', 'Entre 410 y 450 gramos', ['Entre 300 y 350 gramos', 'Entre 500 y 550 gramos', 'Entre 360 y 400 gramos'], 4],
  ['reg', '¿Cuánto mide el área grande desde la línea del arco hacia adentro?', '16,5 metros', ['11 metros', '18 metros', '20 metros'], 4],
  ['reg', '¿Cuánto mide el área chica desde la línea del arco hacia adentro?', '5,5 metros', ['6 metros', '4 metros', '7 metros'], 4],
];
const CATS_TRIVIA = { todas: ['Todas mezcladas', '🎲'], sel: ['Selección y Mundiales', '🇦🇷'], arg: ['Fútbol argentino', '🏟️'],
                      est: ['Estrellas', '⭐'], eur: ['Europa', '🌍'], reg: ['Reglamento', '📏'] };
const NIVELES_TRIVIA = {
  facil: { nombre: 'Fácil', emoji: '🟢', segundos: 20, multi: 1, usa: [1], relleno: [2] },
  medio: { nombre: 'Medio', emoji: '🟡', segundos: 15, multi: 1.5, usa: [2], relleno: [1, 3] },
  dificil: { nombre: 'Difícil', emoji: '🔴', segundos: 10, multi: 2, usa: [3], relleno: [2] },
  imposible: { nombre: 'Imposible', emoji: '💀', segundos: 8, multi: 3, usa: [4], relleno: [3] },
};
let trivia = null;
function mezclar(a) { a = a.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
function vistaTrivia() {
  clearInterval(trivia && trivia.reloj);
  const records = leer('tiki_trivia_records', {});
  const nivel = leer('tiki_trivia_nivel', 'facil');
  const nv = NIVELES_TRIVIA[nivel];
  $('vista').innerHTML = volver('Trivia') + `<main>
    <section class="tarjeta pad" style="text-align:center">
      <div style="font-size:40px">🧠⚽</div>
      <h2 style="margin:6px 0 4px;font-size:20px">Trivia Tiki Data</h2>
      <p class="nota-pie" style="margin:0">10 preguntas · más rápido, más puntos</p></section>
    <section class="tarjeta pad"><b style="font-size:14px">Elegí el nivel</b>
      <div class="niveles-trivia">${Object.entries(NIVELES_TRIVIA).map(([k, n]) => `<button class="nivel-trivia ${k === nivel ? 'on' : ''}" data-nivel="${k}">${n.emoji} ${n.nombre}<small>${n.segundos} seg${n.multi > 1 ? ' · x' + n.multi + ' pts' : ''}</small></button>`).join('')}</div></section>
    <div class="cats-trivia">${Object.entries(CATS_TRIVIA).map(([k, [n, e]]) => `<button class="tarjeta cat-trivia" data-cat="${k}">
      <span style="font-size:26px">${e}</span><b>${n}</b><small>${records[k + '_' + nivel] ? 'Récord ' + nv.nombre.toLowerCase() + ': ' + records[k + '_' + nivel] + ' pts' : 'Sin récord en ' + nv.nombre.toLowerCase()}</small></button>`).join('')}</div></main>`;
  document.querySelectorAll('[data-nivel]').forEach(b => b.onclick = () => { guardar('tiki_trivia_nivel', b.dataset.nivel); vistaTrivia(); });
  document.querySelectorAll('[data-cat]').forEach(b => b.onclick = () => empezarTrivia(b.dataset.cat, nivel));
}
function empezarTrivia(cat, nivel) {
  const nv = NIVELES_TRIVIA[nivel] || NIVELES_TRIVIA.facil;
  const pool = PREGUNTAS.filter(p => cat === 'todas' || p[0] === cat);
  // primero preguntas del nivel elegido; si no alcanzan, se completa con las del nivel de al lado
  // las preguntas que salieron hace poco van al final, así cada partida trae preguntas distintas
  const vistas = leer('tiki_trivia_vistas', []);
  const nueva = (p) => !vistas.includes(p[1]);
  const delNivel = mezclar(pool.filter(p => nv.usa.includes(p[4]))), deAlLado = mezclar(pool.filter(p => nv.relleno.includes(p[4])));
  // orden: del nivel sin repetir, del nivel de al lado sin repetir, y recién después las repetidas
  const elegidas = delNivel.filter(nueva).concat(deAlLado.filter(nueva), delNivel.filter(p => !nueva(p)), deAlLado.filter(p => !nueva(p))).slice(0, 10);
  guardar('tiki_trivia_vistas', vistas.filter(v => !elegidas.some(p => p[1] === v)).concat(elegidas.map(p => p[1])).slice(-200));
  trivia = { cat, nivel, nv, preguntas: mezclar(elegidas), i: 0, puntos: 0, aciertos: 0, racha: 0, reloj: null };
  mostrarPregunta();
}
function mostrarPregunta() {
  const t = trivia, p = t.preguntas[t.i];
  const opciones = mezclar([p[2]].concat(p[3]));
  const total = t.nv.segundos; let quedan = total;
  $('vista').innerHTML = volver('Trivia', '#/trivia') + `<main>
    <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:700;color:var(--suave)">
      <span>${t.nv.emoji} ${t.nv.nombre} · Pregunta ${t.i + 1} de ${t.preguntas.length}</span><span>${t.racha >= 2 ? '🔥 Racha ' + t.racha + ' · ' : ''}${t.puntos} pts</span></div>
    <div class="barra-tiempo"><div id="tiempo"></div></div>
    <section class="tarjeta pad"><small style="color:var(--azul);font-weight:800">${esc(CATS_TRIVIA[p[0]][1] + ' ' + CATS_TRIVIA[p[0]][0].toUpperCase())}</small>
      <h2 style="margin:8px 0 0;font-size:18px;line-height:1.35">${esc(p[1])}</h2></section>
    <div style="display:flex;flex-direction:column;gap:10px">${opciones.map(o => `<button class="opcion-trivia" data-o="${esc(o)}">${esc(o)}</button>`).join('')}</div>
    <div id="segundos" style="text-align:center;font-weight:800;font-size:22px;font-family:Montserrat">${total}</div></main>`;
  const responder = (elegida) => {
    clearInterval(t.reloj);
    const bien = elegida === p[2];
    document.querySelectorAll('.opcion-trivia').forEach(b => {
      b.disabled = true;
      if (b.dataset.o === p[2]) b.classList.add('bien');
      else if (b.dataset.o === elegida) b.classList.add('mal');
    });
    if (bien) { t.racha++; t.aciertos++; t.puntos += Math.round((100 + quedan * 10 + (t.racha >= 3 ? 50 : 0)) * t.nv.multi); } else t.racha = 0;
    $('segundos').innerHTML = bien ? '¡Bien! ✅' : (elegida ? 'Era: ' + esc(p[2]) : '⏰ Se terminó el tiempo');
    setTimeout(() => { if (trivia !== t || !location.hash.startsWith('#/trivia')) return; t.i++; if (t.i < t.preguntas.length) mostrarPregunta(); else finTrivia(); }, 1600);
  };
  document.querySelectorAll('.opcion-trivia').forEach(b => b.onclick = () => responder(b.dataset.o));
  t.reloj = setInterval(() => {
    if (!document.getElementById('segundos')) { clearInterval(t.reloj); return; }
    quedan--; $('segundos').textContent = quedan; $('tiempo').style.width = (quedan / total * 100) + '%';
    if (quedan <= 0) responder(null);
  }, 1000);
}
function finTrivia() {
  const t = trivia, records = leer('tiki_trivia_records', {});
  const clave = t.cat + '_' + t.nivel;
  const nuevo = t.puntos > (records[clave] || 0);
  if (nuevo) { records[clave] = t.puntos; guardar('tiki_trivia_records', records); }
  const frase = t.aciertos === 10 ? '¡Perfecto! Sos un crack 🏆' : t.aciertos >= 7 ? '¡Muy bien! ⭐' : t.aciertos >= 4 ? 'Nada mal 💪' : 'A seguir practicando ⚽';
  const texto = `Hice ${t.puntos} puntos (${t.aciertos}/10) en la Trivia de Tiki Data — ${CATS_TRIVIA[t.cat][0]}, nivel ${t.nv.nombre}. ¿Me superás? ${location.origin}/#/trivia`;
  $('vista').innerHTML = volver('Trivia', '#/trivia') + `<main>
    <section class="tarjeta pad" style="text-align:center">
      <div style="font-size:44px">${t.aciertos >= 7 ? '🏆' : '⚽'}</div>
      <h2 style="margin:6px 0;font-size:22px">${frase}</h2>
      <div style="font-family:Montserrat;font-size:40px;font-weight:900;color:var(--azul)">${t.puntos} pts</div>
      <p style="margin:4px 0 0;color:var(--suave)">${t.nv.emoji} Nivel ${t.nv.nombre} · ${t.aciertos} de 10 correctas${nuevo ? ' · <b style="color:var(--verde)">¡Nuevo récord!</b>' : ''}</p></section>
    <button class="boton" id="otraVez">Jugar otra vez</button>
    <a class="boton" style="background:#25D366;color:#fff" href="https://wa.me/?text=${encodeURIComponent(texto)}" target="_blank" rel="noopener">Desafiar a un amigo por WhatsApp</a>
    <a class="boton sec" href="#/trivia">Elegir otra categoría</a></main>`;
  $('otraVez').onclick = () => empezarTrivia(t.cat, t.nivel);
}

/* ---------- En vivo gratis: transmisiones oficiales de YouTube ---------- */
async function vistaEnVivo(idVideo) {
  $('vista').innerHTML = volver('En vivo gratis') + '<main id="vivo"><div class="cargando">Buscando partidos en vivo…</div></main>';
  let d; try { d = await pedir('/api/envivo'); } catch (e) { $('vivo').innerHTML = error(e); return; }
  const lista = d.partidos || [];
  const elegido = lista.find(x => x.id === idVideo) || null;
  const reproductor = elegido ? `<section class="tarjeta" style="overflow:hidden">
      <div style="position:relative;padding-top:56.25%;background:#000"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(elegido.id)}?autoplay=1&rel=0" title="${esc(elegido.titulo)}" style="position:absolute;inset:0;width:100%;height:100%;border:0" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>
      <div style="padding:10px 12px 12px"><b style="font-size:14px">${esc(elegido.titulo)}</b><br><small style="color:var(--suave)">${esc(elegido.canal)} · <a class="link" href="https://www.youtube.com/watch?v=${encodeURIComponent(elegido.id)}" target="_blank" rel="noopener">Abrir en YouTube ↗</a></small></div></section>` : '';
  const tarjetas = lista.filter(x => x !== elegido).map(x => `<a class="tarjeta video" href="#/envivo/${encodeURIComponent(x.id)}" style="display:block;overflow:hidden">
      <div class="portada-video"><img src="${esc(x.portada)}" alt="" loading="lazy" referrerpolicy="no-referrer"><span class="play">${svg('M8 5v14l11-7z',26,'fill="#fff" stroke="none"')}</span>
        <span style="position:absolute;top:8px;left:8px;background:#E5332A;color:#fff;font-size:11px;font-weight:800;padding:3px 8px;border-radius:6px">● EN VIVO</span></div>
      <div class="info-video"><b>${esc(x.titulo)}</b><small>${esc(x.canal)}</small></div></a>`).join('');
  const vacio = !lista.length ? `<div class="tarjeta aviso">${d.aviso === 'sin_clave' ? 'Falta conectar YouTube.' : 'Ahora no hay partidos gratis en vivo de canales oficiales.<br><small>Suele haber partidos del ascenso, femenino, juveniles y reserva, sobre todo los fines de semana. Volvé a mirar más tarde.</small>'}</div>` : '';
  $('vivo').innerHTML = `<p class="nota-pie" style="margin:0">Partidos que transmiten <b>gratis y en forma oficial</b> ligas, federaciones y clubes en YouTube. La lista se actualiza cada 30 minutos.</p>
    ${reproductor}${tarjetas}${vacio}`;
  if (elegido) window.scrollTo(0, 0);
}

/* ---------- Partido ---------- */
const POS_COL = { ARQ:'#A78BFA', DEF:'#FF5A5F', MED:'#FDBA74', DEL:'#3DD68C', SUP:'#8E9AAE' };
const POS_ABR = { ARQ:'ARQ', DEF:'DFC', MED:'MC', DEL:'DC', SUP:'SUP' };
const POS_NOM = { ARQ:'Arquero', DEF:'Defensor', MED:'Mediocampista', DEL:'Delantero', SUP:'Suplente' };
function posDeTexto(t) { t = (t || '').toLowerCase(); if (t.includes('goal') || t === 'g') return 'ARQ'; if (t.includes('def') || t.includes('back') || t === 'd') return 'DEF'; if (t.includes('mid') || t === 'm') return 'MED'; if (t.includes('att') || t.includes('forw') || t.includes('off') || t.includes('strik') || t.includes('wing') || t === 'f') return 'DEL'; return ''; }
const POS_ES = [[/goal ?keeper|^goalkeeper|^portero/i, 'Arquero'], [/left[- ]?back/i, 'Lateral izquierdo'], [/right[- ]?back/i, 'Lateral derecho'],
  [/wing[- ]?back/i, 'Carrilero'], [/cent(re|er)[- ]?back/i, 'Defensor central'], [/defen[cs]e|defender/i, 'Defensor'],
  [/defensive mid/i, 'Volante central'], [/attacking mid/i, 'Enganche'], [/left mid/i, 'Volante izquierdo'], [/right mid/i, 'Volante derecho'],
  [/cent(re|er)(al)? mid/i, 'Mediocampista central'], [/midfield/i, 'Mediocampista'], [/left wing/i, 'Extremo izquierdo'], [/right wing/i, 'Extremo derecho'],
  [/second striker/i, 'Segundo delantero'], [/cent(re|er)[- ]?forward/i, 'Centrodelantero'], [/forward|striker|attacker|offen[cs]e/i, 'Delantero'],
  [/manager|coach/i, 'Director técnico']];
function posES(t) { if (!t) return ''; const m = POS_ES.find(([re]) => re.test(t)); return m ? m[1] : t; }
function posDeFila(i, n) { return i === 0 ? 'ARQ' : i === 1 ? 'DEF' : i === n - 1 ? 'DEL' : 'MED'; }
// Banderas: código de país -> emoji
function bandera(cod) {
  if (!cod) return '';
  const esp = { 'GB-ENG':'🏴\u{E0067}\u{E0062}\u{E0065}\u{E006E}\u{E0067}\u{E007F}', 'GB-SCT':'🏴\u{E0067}\u{E0062}\u{E0073}\u{E0063}\u{E0074}\u{E007F}', 'GB-WLS':'🏴\u{E0067}\u{E0062}\u{E0077}\u{E006C}\u{E0073}\u{E007F}' };
  if (esp[cod]) return esp[cod];
  return cod.length === 2 ? String.fromCodePoint(...[...cod.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '';
}
const nrm = (t) => String(t || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const minNum = (m) => { const x = String(m || '').match(/(\d+)(?:\s*\+\s*(\d+))?/); return x ? +x[1] + (x[2] ? +x[2] / 100 : 0) : 0; };
const esGol = (t) => /^(goal|penalty)$/i.test(t);
const esRoja = (t) => /red card|second yellow|yellow.?red/i.test(t);
const esAmarilla = (t) => /^yellow card$/i.test(t);
const esCambio = (t) => /substitut/i.test(t);
const esLesion = (t) => /injur|lesi/i.test(t);
const esPalo = (t) => /wood|post|crossbar|bar\b|palo/i.test(t);
// Qué hizo cada jugador en el partido (goles, asistencias, tarjetas, cambios, lesiones)
function eventosPorJugador(p) {
  const m = {};
  const de = (n) => (m[nrm(n)] ||= { goles: [], asist: [], gec: [], am: null, roja: null, entra: null, sale: null, lesion: null, entraPor: '' });
  (p.eventos || []).forEach(e => {
    if (esGol(e.tipo) && e.jugador) de(e.jugador).goles.push(e.minuto);
    if (esGol(e.tipo) && e.asistencia) de(e.asistencia).asist.push(e.minuto);
    if (/own goal/i.test(e.tipo) && e.jugador) de(e.jugador).gec.push(e.minuto);
    if (esAmarilla(e.tipo) && e.jugador) de(e.jugador).am = e.minuto;
    if (esRoja(e.tipo) && e.jugador) de(e.jugador).roja = e.minuto;
    if (esCambio(e.tipo)) { if (e.jugador) de(e.jugador).entra = e.minuto; if (e.sale) { de(e.sale).sale = e.minuto; de(e.sale).entraPor = e.jugador; } }
    if (esLesion(e.tipo) && e.jugador) de(e.jugador).lesion = e.minuto;
  });
  return m;
}
function rojasDe(p, idEq) { return (p.eventos || []).filter(e => esRoja(e.tipo) && String(e.equipo) === String(idEq)).length; }
function colorPuntaje(v) { return v >= 8 ? '#1E9E5A' : v >= 7 ? '#3DD68C' : v >= 6.5 ? '#A3D65C' : v >= 6 ? '#F5C451' : '#FF8A4C'; }
function camisetaNum(c, num) {
  const claro = (h) => { const x = parseInt(String(h).replace('#', '').padEnd(6, '0'), 16); return ((x >> 16) * 299 + ((x >> 8) & 255) * 587 + (x & 255) * 114) / 1000 > 150; };
  const txt = claro(c.c1) ? '#0B1220' : '#FFFFFF';
  return `<svg class="cam" viewBox="0 0 40 36" aria-hidden="true"><path d="M13 2 L6 5 L1 12 L7 16 L9 13 L9 34 L31 34 L31 13 L33 16 L39 12 L34 5 L27 2 Q20 7 13 2 Z" fill="${c.c1}" stroke="${c.c2}" stroke-width="2" stroke-linejoin="round"/>
    <text x="20" y="27" text-anchor="middle" font-family="Inter, sans-serif" font-weight="900" font-size="13" fill="${txt}">${num ?? ''}</text></svg>`;
}
const PALO_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M3 21V5h14v16" fill="none" stroke="#EEF2F7" stroke-width="2.2"/><circle cx="18.5" cy="6.5" r="3.6" fill="#fff" stroke="#0B1220" stroke-width="1.2"/></svg>';
const tarjeta = (roja) => `<i class="tj ${roja ? 'roja' : ''}"></i>`;

let timerPartido = null;
async function vistaPartido(id, pestana, deNuevo) {
  clearTimeout(timerPartido);
  const hashAntes = location.hash;
  if (!deNuevo) $('vista').innerHTML = volver('Partido') + '<div class="cargando">Cargando partido…</div>';
  let p;
  try { p = deNuevo ? await pedirDeVerdad('/api/partido?id=' + id) : await pedir('/api/partido?id=' + id); }
  catch (e) { if (deNuevo) { timerPartido = setTimeout(() => { if (location.hash === hashAntes) vistaPartido(id, pestana, true); }, 25000); return; } $('vista').innerHTML = volver('Partido') + `<main>${error(e)}</main>`; return; }
  if (location.hash !== hashAntes) return;
  const scrollAntes = window.scrollY;
  // en vivo: se actualiza solo cada 25 segundos, sin cambiar de pestaña
  if (p.vivo) timerPartido = setTimeout(() => {
    const otra = () => { if (location.hash !== hashAntes) return; const on = document.querySelector('.pestanas button.on'); vistaPartido(id, on ? on.textContent : pestana, true); };
    if (document.visibilityState === 'visible') otra(); else document.addEventListener('visibilitychange', function f() { if (document.visibilityState === 'visible') { document.removeEventListener('visibilitychange', f); otra(); } });
  }, 25000);
  const jugado = p.gl !== null && p.gl !== undefined;
  const d = new Date(p.fecha + 'T12:00:00');
  const fechaTxt = `${DIAS_L[d.getDay()]} ${d.getDate()} de ${MESES_L[d.getMonth()]} · ${p.hora}`;
  const estado = p.vivo ? `${estadoTexto(p)} · En vivo` : p.terminado ? 'Final' : fechaTxt;
  const esHL = p.fuente === 'hl';
  p.colores = p.colores || { local: { c1: '#2F6BDB', c2: '#fff' }, visitante: { c1: '#E5E7EB', c2: '#111' } };
  const EV = eventosPorJugador(p);
  // goles debajo del marcador, cada uno en su lado
  const golesDe = (idEq) => (p.goles || []).filter(g => String(g.equipo) === String(idEq))
    .map(g => `<div><b>${esc(g.minuto)}'</b> ${esc(g.jugador || 'Gol')}${g.tipo === 'PENALTY' ? ' (p)' : g.tipo === 'OWN' ? ' (e.c.)' : ''}</div>`).join('');
  const gL = golesDe(p.local.id), gV = golesDe(p.visitante.id);
  const rojas = (idEq) => { const n = rojasDe(p, idEq); return n ? `<span class="rojas">${'<i></i>'.repeat(n)}</span>` : ''; };
  // pestañas: solo las que tienen datos
  const pest = [['Resumen', () => panelResumen()]];
  const hayAlin = p.alineacion && p.alineacion.local && p.alineacion.visitante && p.alineacion.local.filas.length;
  if (hayAlin) pest.push(['Formación', () => panelFormacion(p, EV)]);
  if (esHL && (p.vivo || p.terminado)) pest.push(['Estadísticas', () => '<div id="stats"><div class="cargando">Cargando estadísticas…</div></div>']);
  if ((p.eventos || []).length) pest.push(['Minuto a minuto', () => panelMinuto(p)]);
  if (p.historial && p.historial.total) pest.push(['Historial', () => panelHistorial(p)]);
  $('vista').innerHTML = `<div class="volver"><a class="icono" href="javascript:history.back()" aria-label="Volver">${svg(ICON.atras)}</a>
      <a href="#/liga/${p.liga}" style="flex:1;display:flex;flex-direction:column"><b style="font-size:15px">${esc(p.ligaNombre)}</b><small style="color:var(--suave);font-size:12px">${p.jornada ? 'Fecha ' + p.jornada : esc(nombreFase(p.fase))}</small></a></div>
    <section class="marcador">
      <div class="lado"><span class="esc-rojas">${escudo(p.local)}${rojas(p.local.id)}</span>${esc(p.local.nombre)}</div>
      <div><div class="res">${jugado ? p.gl + ' - ' + p.gv : 'vs'}</div><div class="estado ${p.vivo ? 'vivo' : ''}">${p.vivo ? `<span${marcaMinuto(p)}>${esc(estadoTexto(p))}</span> · En vivo` : esc(estado)}</div></div>
      <div class="lado"><span class="esc-rojas">${escudo(p.visitante)}${rojas(p.visitante.id)}</span>${esc(p.visitante.nombre)}</div>
    </section>
    ${pest.length > 1 ? `<div class="pestanas" role="tablist">${pest.map(([t], i) => `<button role="tab" data-p="${i}" class="${i === 0 ? 'on' : ''}">${t}</button>`).join('')}</div>` : ''}
    <main id="panel"></main>`;
  function panelResumen() {
    return `<div id="resumenYT"><div class="portada-video cargando-video"></div></div>
      <section class="tarjeta pad"><h2 style="margin:0 0 4px;font-size:15px">Información</h2>
        <div class="dato"><span>Fecha</span><span>${esc(fechaTxt)}</span></div>
        ${p.medio && p.medio[0] !== null && p.medio[0] !== undefined ? `<div class="dato"><span>Primer tiempo</span><span>${p.medio[0]} - ${p.medio[1]}</span></div>` : ''}
        ${p.estadio ? `<div class="dato"><span>Estadio</span><span>${esc(p.estadio)}</span></div>` : ''}
        ${p.arbitro ? `<div class="dato"><span>Árbitro</span><span>${esc(p.arbitro)}</span></div>` : ''}
      </section>
      ${esHL && p.vivo ? '<p class="nota-pie">Se actualiza solo cada uno o dos minutos mientras se juega.</p>' : ''}`;
  }
  function panelHistorial(p) {
    const h = p.historial;
    return `<section class="tarjeta pad"><h2 style="margin:0 0 10px;font-size:15px">Últimos ${h.total} enfrentamientos</h2>
      <div class="datos3"><div><small>Ganó ${esc(p.local.nombre)}</small><b>${h.local ?? 0}</b></div><div><small>Empates</small><b>${h.empates ?? 0}</b></div><div><small>Ganó ${esc(p.visitante.nombre)}</small><b>${h.visitante ?? 0}</b></div></div></section>
      <section class="tarjeta">${h.partidos.map(x => filaPartido(x, true)).join('')}</section>`;
  }
  const mostrar = (i) => {
    $('panel').innerHTML = pest[i][1]();
    document.querySelectorAll('.pestanas button').forEach(b => b.classList.toggle('on', +b.dataset.p === i));
    if (pest[i][0] === 'Resumen') cargarResumenYT(p.id);
    if (pest[i][0] === 'Estadísticas') cargarStats(p);
    if (pest[i][0] === 'Formación') activarFormacion(p, EV);
  };
  document.querySelectorAll('.pestanas button').forEach(b => b.onclick = () => mostrar(+b.dataset.p));
  const iniPest = pestana ? Math.max(0, pest.findIndex(([t]) => t === pestana)) : 0;
  mostrar(iniPest);
  if (deNuevo) window.scrollTo(0, scrollAntes);
  // si no hay estadísticas, la pestaña se saca sola
  if (esHL && (p.vivo || p.terminado)) pedir('/api/estadisticas?id=' + (p.hlId || p.id)).then(s => {
    if (!s.estadisticas || !s.estadisticas.length) { const b = [...document.querySelectorAll('.pestanas button')].find(x => x.textContent === 'Estadísticas'); if (b && !b.classList.contains('on')) b.remove(); }
  }).catch(() => {});
}

/* Formación: una sola cancha con los dos equipos enfrentados */
const POS_LARGA = { ARQ: 'Arquero', DEF: 'Defensor', MED: 'Mediocampista', DEL: 'Delantero', SUP: 'Suplente' };
let FORM = { lado: 'local', info: {} };
function puntajeDe(p, j) { const v = (p.puntajes || {})[nrm(j.nombre)]; return v ? `<span class="pje" style="background:${colorPuntaje(v)}">${v.toFixed(1)}</span>` : ''; }
function fichaCancha(p, j, ev, col, numeros) {
  const e = ev[nrm(j.nombre)] || {};
  const entra = e.entraPor ? (numeros[nrm(e.entraPor)] ?? '') : '';
  return `<div class="fj">
    <div class="fj-cam">${camisetaNum(col, j.numero)}${puntajeDe(p, j)}
      ${e.roja ? '<i class="tj roja fj-tj"></i>' : e.am ? '<i class="tj fj-tj"></i>' : ''}
      ${e.goles && e.goles.length ? `<span class="fj-gol">${'⚽'.repeat(Math.min(e.goles.length, 3))}</span>` : ''}
      ${e.sale ? `<span class="fj-cambio" title="Salió ${esc(e.sale)}'">${entra !== '' ? entra : '⇄'}</span>` : ''}</div>
    <span class="fj-nom">${esc(String(j.nombre).split(' ').slice(-1)[0])}</span></div>`;
}
function panelFormacion(p, ev) {
  const a = p.alineacion;
  const nums = (eq) => { const m = {}; eq.filas.flat().concat(eq.suplentes).forEach(j => { m[nrm(j.nombre)] = j.numero; }); return m; };
  const nL = nums(a.local), nV = nums(a.visitante);
  const lineas = (eq, col, n, arriba) => { const f = arriba ? eq.filas : eq.filas.slice().reverse();
    return f.map(fila => `<div class="cl">${fila.map(j => fichaCancha(p, j, ev, col, n)).join('')}</div>`).join(''); };
  const barra = (t, eq) => `<div class="eq-barra">${escudo(t)}<b>${esc(t.nombre)}</b><span>${esc(eq.formacion || '')}</span></div>`;
  return `<section class="tarjeta" style="overflow:hidden">
      ${barra(p.visitante, a.visitante)}
      <div class="cancha1"><div class="mitad">${lineas(a.visitante, p.colores.visitante, nV, true)}</div><div class="linea-medio"></div><div class="mitad">${lineas(a.local, p.colores.local, nL, false)}</div></div>
      ${barra(p.local, a.local)}
    </section>
    <div class="chips" style="margin-top:2px"><button class="chip ${FORM.lado === 'local' ? 'on' : ''}" data-flado="local">${escImg(p.local)} ${esc(p.local.nombre)}</button><button class="chip ${FORM.lado === 'visitante' ? 'on' : ''}" data-flado="visitante">${escImg(p.visitante)} ${esc(p.visitante.nombre)}</button></div>
    <div id="listaJug">${listaJugadores(p, ev)}</div>`;
}
function listaJugadores(p, ev) {
  const k = FORM.lado, eq = p.alineacion[k], col = p.colores[k], n = eq.filas.length;
  const fila = (j, pos) => {
    const e = ev[nrm(j.nombre)] || {}, inf = FORM.info[nrm(j.nombre)] || {};
    const marcas = [
      e.am && !e.roja ? `<span class="mk">${tarjeta(false)}${esc(e.am)}'</span>` : '', e.roja ? `<span class="mk">${tarjeta(true)}${esc(e.roja)}'</span>` : '',
      ...(e.goles || []).map(m => `<span class="mk">⚽ ${esc(m)}'</span>`), ...(e.gec || []).map(m => `<span class="mk">⚽ ${esc(m)}' (e.c.)</span>`),
      ...(e.asist || []).map(m => `<span class="mk asis">👟 Asist. ${esc(m)}'</span>`),
      e.sale ? `<span class="mk sale">▼ ${esc(e.sale)}'</span>` : '', e.entra ? `<span class="mk entra">▲ ${esc(e.entra)}'</span>` : '',
      e.lesion ? `<span class="mk">🩹 Lesión ${esc(e.lesion)}'</span>` : ''].join('');
    return `<div class="fjug"><span class="num-c" style="background:${col.c1};color:${col.c2};border-color:${col.c2}">${j.numero ?? '–'}</span>
      <span class="fjug-t"><small>${POS_LARGA[pos] || ''}</small><b>${esc(j.nombre)}</b>${marcas ? `<span class="mks">${marcas}</span>` : ''}</span>
      <span class="fjug-d">${puntajeDe(p, j)}</span></div>`;
  };
  return `<section class="tarjeta"><div class="cab"><div class="t"><b>Titulares</b><small>${esc(p[k].nombre)}${eq.formacion ? ' · ' + esc(eq.formacion) : ''}</small></div></div>
      ${eq.dt ? `<div class="fjug"><span class="num-c dt">DT</span><span class="fjug-t"><small>Dirección técnica</small><b>${esc(eq.dt)}</b></span><span></span></div>` : ''}
      ${eq.filas.flatMap((f, i) => f.map(j => fila(j, posDeTexto(j.posicion) || posDeFila(i, n)))).join('')}</section>
    ${eq.suplentes.length ? `<section class="tarjeta"><div class="cab"><div class="t"><b>Suplentes</b></div></div>${eq.suplentes.map(j => fila(j, posDeTexto(j.posicion) || 'SUP')).join('')}</section>` : ''}`;
}
function activarFormacion(p, ev) {
  document.querySelectorAll('[data-flado]').forEach(b => b.onclick = () => {
    FORM.lado = b.dataset.flado;
    document.querySelectorAll('[data-flado]').forEach(x => x.classList.toggle('on', x === b));
    $('listaJug').innerHTML = listaJugadores(p, ev);
  });
}
async function cargarInfoJugadores(p, ev) {
  const hid = p.hlId || (String(p.id).startsWith('h') ? p.id : null);
  if (!hid) return;
  try {
    const d = await pedir('/api/jugadores_partido?id=' + hid);
    FORM.info = d.jugadores || {};
    if ($('listaJug').isConnected && Object.keys(FORM.info).length) $('listaJug').innerHTML = listaJugadores(p, ev);
  } catch (e) {}
}

// Estadísticas: si alguna no está en la lista, se traduce palabra por palabra
const PALABRAS_ES = { shots:'remates', shot:'remate', on:'al', target:'arco', off:'afuera', blocked:'bloqueados', inside:'dentro del', outside:'fuera del', box:'área',
  corner:'córners', corners:'córners', kicks:'', fouls:'faltas', offsides:'offsides', yellow:'amarillas', red:'rojas', cards:'', goalkeeper:'del arquero', saves:'atajadas',
  passes:'pases', pass:'pases', accurate:'correctos', successful:'exitosos', total:'', possession:'posesión', ball:'', tackles:'quites', interceptions:'intercepciones',
  clearances:'despejes', crosses:'centros', dribbles:'gambetas', duels:'duelos', won:'ganados', aerial:'aéreos', aerials:'aéreos', big:'claras', chances:'chances', missed:'erradas',
  expected:'esperados', goals:'goles', hit:'', woodwork:'tiros al palo', free:'tiros libres', throw:'laterales', ins:'', attacks:'ataques', dangerous:'peligrosos',
  long:'largos', balls:'pelotazos', key:'clave', touches:'toques', in:'en', the:'', of:'de', penalties:'penales', penalty:'penal', saved:'atajados', counter:'contra', assists:'asistencias' };
function statES(n) {
  if (!n) return '';
  if (STATS_ES[n]) return STATS_ES[n];
  const k = Object.keys(STATS_ES).find(x => x.toLowerCase() === n.toLowerCase().replace(/_/g, ' '));
  if (k) return STATS_ES[k];
  // adjetivo + sustantivo en inglés -> sustantivo + adjetivo en castellano
  let ws = n.split(/[\s_]+/);
  if (ws.length === 2 && /^(successful|accurate|blocked|dangerous|total|big|key|long|missed|won)$/i.test(ws[0])) ws = [ws[1], ws[0]];
  const t = ws.map(w => PALABRAS_ES[w.toLowerCase()] ?? w).filter(Boolean).join(' ');
  return t ? t[0].toUpperCase() + t.slice(1) : n;
}
const STATS_ES = { 'Shots inside box':'Remates dentro del área', 'Shots outside box':'Remates fuera del área', 'Hit woodwork':'Tiros al palo', 'Big chances':'Chances claras',
  'Big chances missed':'Chances claras erradas', 'Free kicks':'Tiros libres', 'Throw ins':'Laterales', 'Throw-ins':'Laterales', 'Goal kicks':'Saques de arco', 'Tackles':'Quites',
  'Interceptions':'Intercepciones', 'Clearances':'Despejes', 'Saves':'Atajadas', 'Accurate passes':'Pases correctos', 'Passes %':'Precisión de pases', 'Crosses':'Centros',
  'Dribbles':'Gambetas', 'Successful dribbles':'Gambetas exitosas', 'Duels won':'Duelos ganados', 'Aerials won':'Duelos aéreos ganados', 'Attacks':'Ataques',
  'Dangerous attacks':'Ataques peligrosos', 'Successful passes':'Pases correctos', 'Key passes':'Pases clave', 'Goalkeeper saves':'Atajadas', 'Shots':'Remates', 'Long balls':'Pelotazos', 'Assists':'Asistencias', 'Penalties':'Penales', 'Counter attacks':'Contraataques', 'Ball possession':'Posesión', 'Possession':'Posesión', 'Total shots':'Remates', 'Shots on target':'Remates al arco', 'Shots off target':'Remates afuera', 'Blocked shots':'Remates bloqueados', 'Corner kicks':'Córners', 'Corners':'Córners', 'Fouls':'Faltas', 'Offsides':'Offsides', 'Yellow cards':'Amarillas', 'Red cards':'Rojas', 'Goalkeeper saves':'Atajadas', 'Total passes':'Pases', 'Passes accurate':'Pases correctos', 'Expected goals':'Goles esperados (xG)' };
async function cargarStats(p) {
  const el = $('stats'); if (!el) return;
  try {
    const d = await pedir('/api/estadisticas?id=' + (p.hlId || p.id));
    if (!d.estadisticas.length) { el.innerHTML = '<div class="tarjeta aviso">Todavía no hay estadísticas de este partido.</div>'; return; }
    const num = (v) => parseFloat(String(v ?? '0').replace('%', '').replace(',', '.')) || 0;
    el.innerHTML = `<section class="tarjeta pad"><div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700;color:var(--suave);margin-bottom:6px"><span class="mini-eq">${escImg(p.local)}${esc(p.local.nombre)}</span><span class="mini-eq">${esc(p.visitante.nombre)}${escImg(p.visitante)}</span></div>
      ${d.estadisticas.map(s => { const a = num(s.local), b = num(s.visitante), t = (a + b) || 1; return `<div style="padding:10px 0;border-top:1px solid var(--linea)">
        <div style="display:grid;grid-template-columns:60px 1fr 60px;font-size:14px"><b>${esc(s.local ?? '-')}</b><span style="text-align:center;font-size:12px;color:var(--suave)">${esc(statES(s.nombre))}</span><b style="text-align:right">${esc(s.visitante ?? '-')}</b></div>
        <div class="barra"><div><i style="width:${Math.round(a / t * 100)}%;background:${p.colores.local.c1}"></i></div><div><i style="width:${Math.round(b / t * 100)}%;background:${p.colores.visitante.c1}"></i></div></div></div>`; }).join('')}</section>`;
  } catch (e) { el.innerHTML = error(e); }
}

/* Minuto a minuto: línea de tiempo en el medio, cada equipo de su lado */
const ICO = {
  gol: '<span class="mm2-emo">⚽</span>',
  amarilla: '<svg viewBox="0 0 24 24"><rect x="7" y="3.5" width="10" height="17" rx="2" fill="#F5C400"/></svg>',
  roja: '<svg viewBox="0 0 24 24"><rect x="7" y="3.5" width="10" height="17" rx="2" fill="#FF3B3B"/></svg>',
  doble: '<svg viewBox="0 0 24 24"><rect x="4.5" y="3" width="9" height="15" rx="2" fill="#F5C400"/><rect x="10.5" y="6" width="9" height="15" rx="2" fill="#FF3B3B"/></svg>',
  cambio: '<svg viewBox="0 0 24 24"><path d="M8 20V6m0 0l-3.5 3.5M8 6l3.5 3.5" stroke="#22C55E" stroke-width="2.2" fill="none" stroke-linecap="round"/><path d="M16 4v14m0 0l-3.5-3.5M16 18l3.5-3.5" stroke="#EF4444" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>',
  lesion: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="#C7D2E0" stroke-width="1.8"/><path d="M10.5 6.5h3v4h4v3h-4v4h-3v-4h-4v-3h4z" fill="#C7D2E0"/></svg>',
  palo: '<span class="mm2-palo"><svg viewBox="0 0 38 24"><g stroke="#55637A" stroke-width=".8"><path d="M4.5 10h19M4.5 14h19M4.5 18h19M8 6.5v14.5M12 6.5v14.5M16 6.5v14.5M20 6.5v14.5"/></g><path d="M2.5 22V5.2h22.6V22" fill="none" stroke="#F2F5F9" stroke-width="2.6" stroke-linejoin="round"/></svg><i>⚽</i></span>',
  errado: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="#C7D2E0" stroke-width="1.8"/><path d="M8 8l8 8M16 8l-8 8" stroke="#C7D2E0" stroke-width="2" stroke-linecap="round"/></svg>',
  var: '<svg viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="3" fill="#EEF2F7"/><text x="12" y="15.5" text-anchor="middle" font-family="Inter,sans-serif" font-size="8" font-weight="900" fill="#0B1220">VAR</text></svg>',
  otro: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" fill="#8E9AAE"/></svg>'
};
function describirEvento(e) {
  const t = e.tipo || '';
  if (/own goal/i.test(t)) return { ic: ICO.gol, tit: 'Gol en contra', nom: e.jugador, gol: true };
  if (/missed penalty/i.test(t)) return { ic: ICO.errado, tit: 'Penal errado', nom: e.jugador };
  if (/var/i.test(t)) return { ic: ICO.var, tit: /cancel/i.test(t) ? (/penalty/i.test(t) ? 'Penal anulado por VAR' : 'Gol anulado por VAR') : (/penalty/i.test(t) ? 'Penal cobrado por VAR' : 'Gol confirmado por VAR'), nom: e.jugador };
  if (esGol(t)) return { ic: ICO.gol, tit: /penalty/i.test(t) ? 'Gol de penal' : 'Gol', nom: e.jugador, extra: e.asistencia ? 'Asistencia: ' + e.asistencia : '', gol: true };
  if (esRoja(t)) return /second|yellow/i.test(t) ? { ic: ICO.doble, tit: 'Doble amarilla', nom: e.jugador } : { ic: ICO.roja, tit: 'Tarjeta roja', nom: e.jugador };
  if (esAmarilla(t)) return { ic: ICO.amarilla, tit: 'Tarjeta amarilla', nom: e.jugador };
  if (esCambio(t)) return { ic: ICO.cambio, tit: 'Cambio', entra: e.jugador, sale: e.sale };
  if (esLesion(t)) return { ic: ICO.lesion, tit: 'Lesión', nom: e.jugador };
  if (esPalo(t)) return { ic: ICO.palo, tit: 'Tiro al palo', nom: e.jugador };
  const OTROS = { 'Penalty Saved':'Penal atajado', 'Penalty saved':'Penal atajado', 'Offside':'Offside', 'Corner':'Córner', 'Foul':'Falta', 'Shot on target':'Remate al arco',
    'Shot off target':'Remate afuera', 'Save':'Atajada', 'Free kick':'Tiro libre', 'Kick off':'Inicio', 'Half time':'Entretiempo', 'Full time':'Final', 'Injury time':'Tiempo agregado' };
  return { ic: ICO.otro, tit: OTROS[t] || t, nom: e.jugador };
}
function panelMinuto(p) {
  const lista = (p.eventos || []).slice().sort((a, b) => minNum(a.minuto) - minNum(b.minuto));
  const cL = p.colores.local.c1, cV = p.colores.visitante.c1;
  let gl = 0, gv = 0, segundo = false;
  const filas = lista.map(e => {
    const d = describirEvento(e), loc = String(e.equipo) === String(p.local.id);
    let marcador = '';
    if (d.gol) { const aFavorLocal = /own goal/i.test(e.tipo) ? !loc : loc; aFavorLocal ? gl++ : gv++; marcador = `<span class="mm2-res">${gl}-${gv}</span>`; }
    const cuerpo = d.entra !== undefined
      ? `<b class="mm2-nom"><span class="mm2-in">▲</span> ${esc(d.entra)}</b>${d.sale ? `<span class="mm2-extra"><span class="mm2-out">▼</span> ${esc(d.sale)}</span>` : ''}`
      : `<b class="mm2-nom">${esc(d.nom || '')}</b>${d.extra ? `<span class="mm2-extra">${esc(d.extra)}</span>` : ''}`;
    const tarjeta = `<div class="mm2-card ${loc ? 'izq' : 'der'} ${d.gol ? 'gol' : ''}" style="--ce:${loc ? cL : cV}"><span class="mm2-ic">${d.ic}</span>
      <span class="mm2-txt"><small>${esc(d.tit)}${marcador}</small>${cuerpo}</span></div>`;
    let sep = '';
    if (!segundo && minNum(e.minuto) > 45.99 && !/^45/.test(String(e.minuto))) { segundo = true; sep = '<div class="mm2-sep"><span>Segundo tiempo</span></div>'; }
    return sep + `<div class="mm2-fila">${loc ? tarjeta : '<span></span>'}<span class="mm2-min">${esc(e.minuto)}'</span>${loc ? '<span></span>' : tarjeta}</div>`;
  }).join('');
  return `<section class="tarjeta mm2">
    <div class="mm2-cab"><span class="mm2-eq">${escImg(p.local)}<b>${esc(p.local.nombre)}</b></span><span class="mm2-eq der"><b>${esc(p.visitante.nombre)}</b>${escImg(p.visitante)}</span></div>
    <div class="mm2-cuerpo"><div class="mm2-sep"><span>Primer tiempo</span></div>${filas}
      ${p.terminado ? `<div class="mm2-sep fin"><span>Final · ${p.gl ?? gl} - ${p.gv ?? gv}</span></div>` : p.vivo ? '<div class="mm2-sep vivo"><span>En juego</span></div>' : ''}</div></section>`;
}

/* ---------- Fechas de un torneo: en orden de calendario y arrancando en la que se juega ahora ---------- */
const FASES_ES = [[/round of 64|1\/32/i, '32avos de final'], [/round of 32|1\/16|last[ _]32/i, '16avos de final'], [/round of 16|1\/8|last[ _]16|octavos/i, 'Octavos de final'],
  [/quarter|cuartos/i, 'Cuartos de final'], [/semi/i, 'Semifinales'], [/3rd|third/i, 'Tercer puesto'], [/^final$|^finals?$|(^|[^-])\bfinal\b$/i, 'Final'],
  [/league[ _]stage/i, 'Fase liga'], [/preliminary|qualif/i, 'Fase previa'], [/^1st round|first round/i, 'Primera ronda'], [/^2nd round|second round/i, 'Segunda ronda'], [/^3rd round|third round/i, 'Tercera ronda'], [/knockout/i, 'Eliminación directa'], [/group[ _]stage/i, 'Fase de grupos'], [/play-?off/i, 'Repechaje'], [/regular[ _]season/i, 'Fase regular']];
function nombreFase(f) {
  const t = String(f || '').replaceAll('_', ' ').trim();
  const g = t.match(/^(?:group|grupo|zona)\s*([a-z0-9]{1,2})\b\s*-?\s*(\d+)?/i);
  if (g) return 'Grupo ' + g[1].toUpperCase() + (g[2] ? ' · Fecha ' + g[2] : '');
  const num = t.match(/(?:season|fecha|round|jornada|matchday)\s*-?\s*(\d+)\s*$/i) || t.match(/-\s*(\d+)\s*$/);
  if (num && !/of \d+/i.test(t)) return 'Fecha ' + num[1];
  for (const [re, es] of FASES_ES) if (re.test(t)) return es;
  return t ? t[0].toUpperCase() + t.slice(1).toLowerCase() : 'Partidos';
}
// Torneos cortos dentro del mismo año: nombre según la mitad del año
const MITADES = { LPF: ['Apertura', 'Clausura'], PAR: ['Apertura', 'Clausura'], PER: ['Apertura', 'Clausura'], COL: ['Apertura', 'Finalización'], LMX: ['Clausura', 'Apertura'], BOL: ['Apertura', 'Clausura'], ECU: ['1ª etapa', '2ª etapa'] };
function armarFechas(partidos, cod) {
  const t = (p) => new Date(p.fecha + 'T' + (p.hora || '12:00') + ':00').getTime();
  const lista = partidos.slice().sort((x, y) => t(x) - t(y));
  const grupos = [];
  for (const p of lista) {
    const nombre = p.jornada ? 'Fecha ' + p.jornada : nombreFase(p.fase);
    // la misma "Fecha 3" del Apertura y del Clausura son grupos distintos: se separan si están lejos en el tiempo
    let g = null;
    for (let k = grupos.length - 1; k >= 0 && k >= grupos.length - 6; k--) {
      if (grupos[k].nombre === nombre && Math.abs(t(p) - grupos[k].ultimo) < 25 * 86400000) { g = grupos[k]; break; }
    }
    if (!g) { g = { nombre, partidos: [], primero: t(p), ultimo: t(p) }; grupos.push(g); }
    g.partidos.push(p); g.ultimo = Math.max(g.ultimo, t(p));
  }
  grupos.sort((x, y) => x.primero - y.primero);
  // si un nombre se repite en el año, se aclara de qué torneo es
  const cuenta = {}; grupos.forEach(g => { cuenta[g.nombre] = (cuenta[g.nombre] || 0) + 1; });
  grupos.forEach(g => {
    if (cuenta[g.nombre] > 1) {
      const d = new Date(g.primero), m = MITADES[cod];
      g.nombre += ' · ' + (m ? m[d.getMonth() < 6 ? 0 : 1] : MESES[d.getMonth()] + ' ' + d.getFullYear());
    }
  });
  // la que se muestra primero: la que tiene un partido en vivo, si no la del próximo partido, si no la última jugada
  const ahora = Date.now(), valido = (p) => !['POSTPONED', 'CANCELLED', 'SUSPENDED'].includes(p.estado);
  let actual = grupos.findIndex(g => g.partidos.some(p => p.vivo));
  if (actual < 0) actual = grupos.findIndex(g => g.partidos.some(p => !p.terminado && valido(p) && t(p) > ahora - 3 * 3600000));
  if (actual < 0) actual = grupos.length - 1;
  return { grupos, actual };
}
async function vistaLiga(cod, pestana) {
  pestana = pestana || 'partidos';
  $('vista').innerHTML = volver(LIGAS[cod][0]) + `<div class="pestanas">${[['partidos','Partidos'],['tabla','Tabla'],['goleadores','Jugadores']].map(([t, n]) => `<button data-t="${t}" class="${t === pestana ? 'on' : ''}">${n}</button>`).join('')}</div><main id="panel"><div class="cargando">Cargando…</div></main>`;
  document.querySelectorAll('.pestanas button').forEach(b => b.onclick = () => { location.hash = `#/liga/${cod}/${b.dataset.t}`; });
  try {
    if (pestana === 'tabla') {
      const [d, cal] = await Promise.all([pedir('/api/tabla?liga=' + cod), pedir('/api/calendario?liga=' + cod).catch(() => null)]);
      const llaves = cal ? cuadroPlayoffs(cal.partidos, cod) : '';
      $('panel').innerHTML = llaves + (d.grupos.length ? d.grupos.map(tablaGrupo).join('') : (llaves ? '' : '<div class="tarjeta aviso">No hay tabla para este torneo.</div>'));
    } else if (pestana === 'goleadores') {
      const d = await pedir('/api/goleadores?liga=' + cod);
      $('panel').innerHTML = listaGoleadores(d, cod);
      cargarFotos($('panel'));
    } else {
      const d = await pedir('/api/calendario?liga=' + cod);
      const { grupos: bloques, actual: inicial } = armarFechas(d.partidos, cod);
      if (!bloques.length) { $('panel').innerHTML = '<div class="tarjeta aviso">Todavía no hay partidos cargados para este torneo.</div>'; return; }
      const porJornada = {}, claves = [], nombres = {};
      bloques.forEach((g, k) => { porJornada[k] = g.partidos; claves.push(k); nombres[k] = g.nombre; });
      let actual = inicial;
      const dibujar = () => {
        const i = claves.indexOf(actual);
        const nombre = nombres[actual];
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

/* ---------- Tablas: zonas (copas, playoffs, descenso) y próximo partido ---------- */
const ZONA_COL = { cl: '#2F6BDB', lib: '#2F6BDB', uel: '#F28C28', sud: '#F28C28', uecl: '#22B07D', asc: '#22B07D', playoff: '#38BDF8', reducido: '#A3E635', promo: '#EAB308', desc: '#EF4444', elim: '#5B6578' };
function tablaGrupo(g) {
  const zonas = g.zonas || [];
  const hayProx = g.filas.some(f => f.prox);
  const prox = (f) => {
    if (!f.prox) return '<td class="prox"></td>';
    const d = new Date(f.prox.fecha + 'T12:00:00');
    return `<td class="prox"><a href="#/partido/${f.prox.id}" title="${f.prox.local ? 'Local' : 'Visitante'} vs ${esc(f.prox.rival.nombre)} · ${d.getDate()}/${d.getMonth() + 1}">${escudo({ ...f.prox.rival, id: null }, 'esc mini')}<small>${f.prox.local ? 'L' : 'V'}</small></a></td>`;
  };
  return `<section class="tarjeta">${g.nombre ? `<div class="cab"><div class="t"><b>${esc(g.nombre)}</b></div></div>` : ''}
    <table class="tabla"><thead><tr><th>#</th><th class="izq">Equipo</th><th>PJ</th><th>G</th><th>E</th><th>P</th><th>DG</th><th>PTS</th>${hayProx ? '<th>Próx.</th>' : ''}</tr></thead><tbody>
    ${g.filas.map((f, i) => {
      const z = f.zona || '', zAnt = (g.filas[i - 1] || {}).zona || '';
      return `<tr class="${z && z !== zAnt && i ? 'corte' : ''} ${z === 'desc' && zAnt !== 'desc' ? 'corte-desc' : ''}"><td class="zpos" style="--z:${ZONA_COL[z] || 'transparent'}"><span class="pos ${z ? 'z' : ''}" style="${z ? `background:${ZONA_COL[z]};color:#0B1220` : ''}">${f.pos}</span></td><td class="izq"><span class="eqt">${escudo(f.equipo)}<span class="nom">${esc(f.equipo.nombre)}</span></span></td><td>${f.pj}</td><td>${f.g}</td><td>${f.e}</td><td>${f.p}</td><td>${f.dg > 0 ? '+' : ''}${f.dg}</td><td class="pts">${f.pts}</td>${hayProx ? prox(f) : ''}</tr>`;
    }).join('')}
    </tbody></table>
    ${zonas.length ? `<div class="zonas">${zonas.map(z => `<span><i style="background:${ZONA_COL[z.tipo]}"></i>${esc(z.texto)}</span>`).join('')}</div>` : ''}</section>`;
}

/* ---------- Cuadro de playoffs: una mitad a la izquierda, la otra a la derecha y la final en el medio ---------- */
const RONDAS_PO = ['Octavos de final', 'Cuartos de final', 'Semifinales', 'Final'];
function cuadroPlayoffs(partidos, cod) {
  const t = (p) => new Date(p.fecha + 'T' + (p.hora || '12:00') + ':00').getTime();
  const ps = (partidos || []).filter(p => !p.jornada && RONDAS_PO.includes(nombreFase(p.fase)));
  if (!ps.length) return '';
  // solo los playoffs del torneo actual (o del que acaba de terminar)
  const ultimo = Math.max(...ps.map(t));
  if (ultimo < Date.now() - 25 * 86400000) return '';
  const rec = ps.filter(p => ultimo - t(p) < 120 * 86400000).sort((a, b) => t(a) - t(b));
  const k = (e) => e && (e.id != null ? String(e.id) : e.nombre);
  let rondas = RONDAS_PO.map(r => {
    const m = {};
    rec.filter(p => nombreFase(p.fase) === r).forEach(p => {
      const c = [k(p.local), k(p.visitante)].sort().join('|');
      (m[c] ||= { eqs: [p.local, p.visitante], partidos: [] }).partidos.push(p);
    });
    return Object.values(m);
  });
  const ini = rondas.findIndex(r => r.length);
  if (ini < 0) return '';
  rondas = rondas.slice(ini);
  const nombresR = RONDAS_PO.slice(ini);
  if (rondas.length < 2) return '';
  // completar con llaves "a definir" las rondas que todavía no se jugaron
  rondas.forEach((r, i) => { const falta = Math.pow(2, rondas.length - 1 - i) - r.length; for (let j = 0; j < falta; j++) r.push({ eqs: [null, null], partidos: [] }); });
  // goles (suma de ida y vuelta) y quién pasó
  rondas.forEach((r, i) => r.forEach(ll => {
    ll.goles = ll.eqs.map(e => e ? ll.partidos.reduce((s, p) => s + (p.gl == null ? 0 : k(p.local) === k(e) ? p.gl : p.gv), 0) : null);
    ll.jugado = ll.partidos.some(p => p.terminado || p.vivo);
    const sig = rondas[i + 1] || [];
    const paso = ll.eqs.find(e => e && sig.some(x => x.eqs.some(y => y && k(y) === k(e))));
    const fin = ll.partidos.length && ll.partidos.every(p => p.terminado);
    ll.ganador = paso ? k(paso) : (fin && ll.goles[0] !== ll.goles[1] ? k(ll.eqs[ll.goles[0] > ll.goles[1] ? 0 : 1]) : null);
  }));
  // ordenar cada ronda según la siguiente, así las llaves quedan conectadas
  for (let i = rondas.length - 2; i >= 0; i--) {
    const libres = rondas[i].slice(), orden = [];
    rondas[i + 1].forEach(sig => sig.eqs.forEach(e => {
      const j = e ? libres.findIndex(ll => ll.eqs.some(x => x && k(x) === k(e))) : -1;
      orden.push(j >= 0 ? libres.splice(j, 1)[0] : null);
    }));
    rondas[i] = orden.map(x => x || libres.shift()).filter(Boolean).concat(libres);
  }
  const caja = (ll, lado) => {
    const fila = (n) => {
      const e = ll.eqs[n];
      const gano = ll.ganador && e && k(e) === ll.ganador, perdio = ll.ganador && e && k(e) !== ll.ganador;
      return `<span class="po-eq ${gano ? 'gana' : ''} ${perdio ? 'pierde' : ''}">${e ? escudo({ ...e, id: null }, 'esc mini') : '<span class="po-nd">?</span>'}<b>${ll.jugado && e ? ll.goles[n] : ''}</b></span>`;
    };
    const p0 = ll.partidos[ll.partidos.length - 1];
    const tag = p0 ? `a href="#/partido/${p0.id}"` : 'div';
    return `<${tag} class="po-llave ${lado}">${fila(0)}${fila(1)}</${p0 ? 'a' : 'div'}>`;
  };
  const n = rondas.length;
  const col = (r, lado, i) => `<div class="po-col" title="${esc(nombresR[i])}">${r.map(ll => caja(ll, lado)).join('')}</div>`;
  const izq = rondas.slice(0, n - 1).map((r, i) => col(r.slice(0, r.length / 2), 'izq', i)).join('');
  const der = rondas.slice(0, n - 1).map((r, i) => col(r.slice(r.length / 2), 'der', i)).reverse().join('');
  const final = rondas[n - 1][0];
  const campeon = final && final.ganador ? final.eqs.find(e => e && k(e) === final.ganador) : null;
  const centro = `<div class="po-col po-centro"><span class="po-copa">🏆</span>${caja(final, 'fin')}${campeon ? `<small class="po-camp">Campeón<br><b>${esc(campeon.nombre)}</b></small>` : '<small class="po-camp">Final</small>'}</div>`;
  const CORTO = { 'Octavos de final': '8vos', 'Cuartos de final': '4tos', 'Semifinales': 'Semis' };
  const etis = nombresR.slice(0, n - 1).map(x => `<span>${esc(CORTO[x] || x)}</span>`).join('');
  const m = MITADES[cod], d0 = new Date(ultimo);
  return `<section class="tarjeta po"><div class="cab"><div class="t"><b>Playoffs${m ? ' · ' + m[d0.getMonth() < 6 ? 0 : 1] : ''}</b><small>Tocá una llave para ver el partido</small></div></div>
    <div class="po-etis" style="grid-template-columns:repeat(${2 * n - 1},1fr)">${etis}<span>Final</span>${etis.split('</span>').filter(Boolean).reverse().map(x => x + '</span>').join('')}</div>
    <div class="po-cuadro" style="grid-template-columns:repeat(${2 * n - 1},1fr)">${izq}${centro}${der}</div></section>`;
}

/* ---------- Calendario de todo el año ---------- */
const cal = { liga: 'ALL', mes: new Date().getMonth(), anio: new Date().getFullYear(), dia: new Date().getDate(), datos: {}, meses: {}, faltan: {} };
async function vistaCalendario() {
  $('vista').innerHTML = volver('Calendario') + `<main id="panel"><div class="cargando">Armando el calendario de todas las ligas…</div></main>`;
  cal.liga = 'ALL';
  dibujarCalendario();
}
// Un solo pedido por mes con todas las ligas. Si alguna liga todavía se está preparando, se completa sola en unos segundos.
async function cargarMes(anio, mes, reintento = 0) {
  const k = anio + '-' + mes, ruta = `/api/calendario_mes?anio=${anio}&mes=${mes + 1}`;
  let d;
  try { d = reintento ? await pedirDeVerdad(ruta) : await pedir(ruta); } catch (e) { if (!cal.meses[k]) cal.meses[k] = []; return; }
  cal.meses[k] = d.partidos; cal.faltan[k] = d.faltan.length;
  if (d.faltan.length && reintento < 20) setTimeout(() => cargarMes(anio, mes, reintento + 1).then(() => {
    if (location.hash.startsWith('#/calendario') && cal.anio === anio && cal.mes === mes) { const y = window.scrollY; dibujarCalendario(); window.scrollTo(0, y); }
  }), 4000);
}
const ORDEN_LIGAS = ['LPF','LIB','SUD','CL','PL','PD','UEL','SA','BL1','FL1','CAR','UECL','BSA','PNA','WC','CAM','EURO',
  'PPL','DED','ELC','MLS','LMX','URU','CHI','COL','PAR','PER','ECU','BOL','VEN','SAU','FAC','CDR','CIT','DFB','CDF','TDC','SCA','FEDA','PRC','PRD'];
function dibujarCalendario() {
  const km = cal.anio + '-' + cal.mes;
  if (!cal.meses[km]) {
    if (!$('panel').querySelector('.cargando')) $('panel').innerHTML = '<div class="cargando">Armando el calendario de todas las ligas…</div>';
    const a = cal.anio, m = cal.mes;
    cargarMes(a, m).then(() => { if (location.hash.startsWith('#/calendario') && cal.anio === a && cal.mes === m) dibujarCalendario(); });
    return;
  }
  const porDia = {};
  cal.meses[km].filter(p => LIGAS[p.liga]).forEach(p => { (porDia[p.fecha] ||= []).push(p); });
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (d) => `${cal.anio}-${pad(cal.mes + 1)}-${pad(d)}`;
  const primero = (new Date(cal.anio, cal.mes, 1).getDay() + 6) % 7;
  const total = new Date(cal.anio, cal.mes + 1, 0).getDate();
  let celdas = ['L','M','M','J','V','S','D'].map(x => `<span class="dsem">${x}</span>`).join('');
  for (let i = 0; i < primero; i++) celdas += '<span></span>';
  for (let d = 1; d <= total; d++) {
    const ps = porDia[iso(d)] || [];
    const esHoy = iso(d) === hoyISO();
    celdas += `<button class="cdia ${ps.length ? 'con' : ''} ${esHoy ? 'hoy' : ''} ${d === cal.dia ? 'on' : ''}" data-d="${d}" aria-label="${d} de ${MESES_L[cal.mes]}${ps.length ? ', ' + ps.length + ' partidos' : ''}">${d}<span class="ps">${ps.length ? `<em class="cant">${ps.length}</em>` : ''}</span></button>`;
  }
  const delDia = (porDia[iso(cal.dia)] || []).sort((a, b) => a.hora.localeCompare(b.hora));
  // partidos del día agrupados por liga, en el orden del menú
  const orden = ORDEN_LIGAS.filter(c => LIGAS[c]).concat(Object.keys(LIGAS).filter(c => !ORDEN_LIGAS.includes(c)));
  const porLiga = orden.map(c => [c, delDia.filter(p => p.liga === c)]).filter(([, ps]) => ps.length);
  const bloques = porLiga.map(([c, ps]) => `<section class="tarjeta cal-liga" style="--cl:${COLOR[c] || '#4F8FF7'}">
      <a class="cab-liga" href="#/liga/${c}"><b>${esc(LIGAS[c][0])}</b><small>${esc(LIGAS[c][1] || '')}${LIGAS[c][1] ? ' · ' : ''}${ps.length} ${ps.length === 1 ? 'partido' : 'partidos'}</small></a>
      ${ps.map(p => filaPartido(p)).join('')}</section>`).join('');
  const fd = new Date(cal.anio, cal.mes, cal.dia);
  $('panel').innerHTML = `<div class="meses">${MESES.map((m, i) => `<button class="mes ${i === cal.mes ? 'on' : ''}" data-m="${i}">${m}</button>`).join('')}</div>
    <section class="tarjeta pad">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <button class="icono" id="mAnt" aria-label="Mes anterior">${svg(ICON.atras,18)}</button>
        <b class="titulo" style="font-size:17px">${MESES_L[cal.mes][0].toUpperCase() + MESES_L[cal.mes].slice(1)} ${cal.anio}</b>
        <button class="icono" id="mSig" aria-label="Mes siguiente">${svg(ICON.sig,18)}</button></div>
      <div class="cal">${celdas}</div></section>
    ${cal.faltan[km] ? `<p class="nota-pie" style="margin:0;text-align:center">⏳ Sumando ${cal.faltan[km]} ligas más…</p>` : ''}
    <div class="cal-dia"><b>${DIAS_L[fd.getDay()][0].toUpperCase() + DIAS_L[fd.getDay()].slice(1)} ${cal.dia} de ${MESES_L[cal.mes]}</b><small>${delDia.length ? delDia.length + ' partidos en ' + porLiga.length + (porLiga.length === 1 ? ' liga' : ' ligas') : 'Sin partidos'}</small></div>
    ${bloques || '<div class="tarjeta aviso">No hay partidos este día.</div>'}`;
  document.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { cal.liga = b.dataset.l; dibujarCalendario(); });
  document.querySelectorAll('[data-m]').forEach(b => b.onclick = () => { cal.mes = +b.dataset.m; cal.dia = 1; dibujarCalendario(); });
  document.querySelectorAll('[data-d]').forEach(b => b.onclick = () => { cal.dia = +b.dataset.d; dibujarCalendario(); });
  $('mAnt').onclick = () => { cal.mes--; if (cal.mes < 0) { cal.mes = 11; cal.anio--; } cal.dia = 1; dibujarCalendario(); };
  $('mSig').onclick = () => { cal.mes++; if (cal.mes > 11) { cal.mes = 0; cal.anio++; } cal.dia = 1; dibujarCalendario(); };
}

/* ---------- Jugadores: goleadores, perfil y comparador ---------- */
const cacheGol = {};
async function goleadores(cod) { if (!cacheGol[cod]) { const d = await pedir('/api/goleadores?liga=' + cod); if (d.calculando) return d; cacheGol[cod] = d; } return cacheGol[cod]; }
// Estadísticas de jugadores de una liga: goleadores, asistidores, más amarillas y más rojas, todo junto
let STAT_TIPO = 'goleadores';
const TIPOS_STAT = [['goleadores', 'Goleadores', 'goles', 'goles'], ['asistidores', 'Asistencias', 'asistencias', 'asist.'],
  ['amarillas', 'Amarillas', 'amarillas', 'amarillas'], ['rojas', 'Rojas', 'rojas', 'rojas']];
function listaGoleadores(d, cod, contenedor) {
  cacheGol[cod] = d;
  if (d.calculando) { delete cacheGol[cod]; return '<div class="tarjeta aviso">Estamos contando los goles y las tarjetas de todos los partidos del torneo. En unos minutos aparece.</div>'; }
  const hay = TIPOS_STAT.filter(([k]) => (d[k] || []).length);
  if (!hay.length) return `<div class="tarjeta aviso">${HL.includes(cod) && !CON_GOL.includes(cod) ? 'Las estadísticas de jugadores de este torneo llegan más adelante.' : 'Todavía no hay estadísticas de jugadores.'}</div>`;
  if (!hay.some(([k]) => k === STAT_TIPO)) STAT_TIPO = hay[0][0];
  const [k, , campo, unidad] = TIPOS_STAT.find(([x]) => x === STAT_TIPO);
  const icono = { goleadores: '⚽', asistidores: '👟', amarillas: '<i class="tj"></i>', rojas: '<i class="tj roja"></i>' }[k];
  setTimeout(() => document.querySelectorAll('[data-stat]').forEach(b => b.onclick = () => {
    STAT_TIPO = b.dataset.stat; const el = b.closest('.stats-jug'); if (el) el.outerHTML = listaGoleadores(d, cod); }), 0);
  return `<div class="stats-jug"><div class="chips" style="margin-bottom:10px">${hay.map(([x, nom]) => `<button class="chip ${x === k ? 'on' : ''}" data-stat="${x}">${nom}</button>`).join('')}</div>
    <section class="tarjeta">${d[k].map((j, i) => `<a class="jug" href="#/jugador/${cod}/${j.id}">
    <span class="pos ${i < 3 ? 'top' : ''}">${i + 1}</span><span class="esc-jug">${escudo(j.equipo, 'esc')}</span>
    <span style="min-width:0"><b class="nom" style="display:block;font-size:14px">${esc(j.nombre)}</b><small style="color:var(--suave);display:block">${esc(j.equipo.nombre)}</small></span>
    <span style="text-align:right"><span class="goles">${icono} ${j[campo]}</span><br><small style="color:var(--suave)">${unidad}</small></span></a>`).join('')}</section></div>`;
}
let ligaJug = LIGAS.LPF ? 'LPF' : 'PD';
function ligasGol() { return CON_GOL.filter(c => LIGAS[c]).concat(Object.keys(LIGAS).filter(c => !HL.includes(c))); }
function edad(n) { if (!n) return '-'; const d = new Date(n), h = new Date(); let a = h.getFullYear() - d.getFullYear(); if (h < new Date(h.getFullYear(), d.getMonth(), d.getDate())) a--; return a + ' años'; }
const POS = { Offence:'Delantero', Midfield:'Mediocampista', Defence:'Defensor', Goalkeeper:'Arquero' };
async function vistaJugador(cod, id) {
  $('vista').innerHTML = volver('Jugador', 'javascript:history.back()') + '<div class="cargando">Cargando…</div>';
  let d; try { d = await goleadores(cod); } catch (e) { $('vista').innerHTML = volver('Jugador') + `<main>${error(e)}</main>`; return; }
  const j = [...d.goleadores, ...(d.asistidores || []), ...(d.amarillas || []), ...(d.rojas || [])].find(x => String(x.id) === String(id));
  if (!j) { $('vista').innerHTML = volver('Jugador') + '<main><div class="tarjeta aviso">No encontramos al jugador.</div></main>'; return; }
  const gpp = j.partidos ? (j.goles / j.partidos).toFixed(2).replace('.', ',') : '-';
  $('vista').innerHTML = volver('Jugador', 'javascript:history.back()') + `<section style="background:var(--negro);padding:6px 16px 18px;border-bottom:1px solid var(--linea);display:flex;flex-direction:column;gap:16px">
      <div style="display:flex;align-items:center;gap:14px"><span class="esc-jug xl">${escudo(j.equipo, 'esc')}</span>
        <div style="display:flex;flex-direction:column;gap:6px"><b class="titulo" style="font-size:22px">${esc(j.nombre)}</b>
        <span style="font-weight:600">${esc(j.equipo.nombre)}</span></div></div>
      ${(() => { const dd = [['Nacionalidad', j.nacionalidad], ['Edad', j.nacimiento ? edad(j.nacimiento) : ''], ['Posición', POS[j.posicion] || posES(j.posicion)]].filter(x => x[1]);
        return dd.length ? `<div class="datos3">${dd.map(([t, v]) => `<div><small>${t}</small><b>${esc(v)}</b></div>`).join('')}</div>` : ''; })()}</section>
    <main><section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">${esc(d.nombre)} · temporada actual</h2>
      <div class="datos3"><div><small>Goles</small><b>${j.goles}</b></div><div><small>Asistencias</small><b>${j.asistencias}</b></div><div><small>Partidos</small><b>${j.partidos}</b></div>
      <div><small>Penales</small><b>${j.penales}</b></div><div><small>Goles por partido</small><b>${gpp}</b></div><div><small>Ranking</small><b>${d.goleadores.indexOf(j) >= 0 ? (d.goleadores.indexOf(j) + 1) + "º" : "-"}</b></div></div></section>
</main>`;
}
/* ---------- Favoritos ---------- */
let ligaFav = 'PD';
async function vistaFavoritos() {
  const favs = leer('tiki_favs', []);
  $('vista').innerHTML = volver('Mis clubes favoritos') + `<main>
    <p class="nota-pie" style="margin:0">Tocá la estrella de los clubes que querés seguir. Sus partidos aparecen primero en el Inicio. Se guardan en este dispositivo.</p>
    ${bloqueAvisos()}
    ${favs.length ? `<section class="tarjeta pad"><b style="font-size:14px">Elegidos (${favs.length})</b><div class="chips" style="margin-top:10px">${favs.map(f => `<button class="chip on" data-quitar="${f.id}">${escudo(f)}${esc(f.nombre)} ✕</button>`).join('')}</div></section>` : ''}
    <div class="chips">${Object.keys(LIGAS).filter(c => !['CL', 'WC', 'CAR', 'LIB', 'SUD', 'UEL', 'UECL', 'FAC', 'CDR', 'CIT', 'DFB', 'CDF', 'CAM', 'EURO'].includes(c)).map(c => `<button class="chip ${c === ligaFav ? 'on' : ''}" data-l="${c}">${esc(LIGAS[c][0])}</button>`).join('')}</div>
    <div id="clubes"><div class="cargando">Cargando clubes…</div></div></main>`;
  activarBloqueAvisos(vistaFavoritos);
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
  const aviso = 0;
  let c; try { c = await pedir('/api/club?id=' + id); } catch (e) { clearTimeout(aviso); $('vista').innerHTML = volver('Club') + `<main>${error(e)}</main>`; return; }
  clearTimeout(aviso);
  dibujarClub(c);
  pedir('/api/club_extra?id=' + id).then(x => {
    if (location.hash !== '#/club/' + id) return;
    Object.entries(x.fotos || {}).forEach(([n, f]) => { FOTOS[n] = f; });
    if (x.plantel && x.plantel.length && !c.plantel.length) { c.plantel = x.plantel; c.plantelNota = x.plantelNota || ''; }
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
      ${c.plantel.length ? `<section class="tarjeta"><div class="cab"><div class="t"><b>Plantel actual</b><small>${c.plantelNota ? esc(c.plantelNota) + ' · ' : ''}${c.plantel.length} jugadores</small></div></div>
        ${orden.filter(g => grupos[g]).map(g => `<div style="padding:10px 12px 2px;font-size:11px;font-weight:800;letter-spacing:.6px;color:var(--suave)">${g.toUpperCase()}</div>` +
          grupos[g].map(j => `<div class="jug" style="grid-template-columns:40px 1fr auto">${caraHTML(j)}<span><b style="font-size:14px">${esc(j.nombre)}</b>${j.nacionalidad ? `<br><small style="color:var(--suave)">${esc(j.nacionalidad)}</small>` : ''}</span><small style="color:var(--suave)">${j.nacimiento ? edad(j.nacimiento) : ''}</small></div>`).join('')).join('')}</section>` : ''}
      ${c.descripcion ? `<section class="tarjeta pad"><h2 style="margin:0 0 8px;font-size:15px">Historia</h2><p style="margin:0;font-size:13px;line-height:1.55;color:#C7D2E0">${esc(c.descripcion)}…</p></section>` : ''}
      <p class="nota-pie">Datos de football-data.org y Highlightly. Clubes argentinos: datos revisados por Tiki Data.</p>
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
// Camiseta pixel art (20 x 20 cuadraditos) con sombras, cuello en V y puños
function camisetaPixel(x) {
  const W = 20, H = 20, celdas = {};
  const pon = (cx, cy, t) => { celdas[cx + ',' + cy] = t; };
  // mangas (en diagonal) y hombros
  const mangaIzq = { 2: [3, 4], 3: [2, 4], 4: [1, 4], 5: [1, 4], 6: [1, 4], 7: [1, 4] };
  Object.entries(mangaIzq).forEach(([cy, [a, b]]) => { for (let cx = a; cx <= b; cx++) { pon(cx, +cy, +cy === 7 ? 'p' : 'm'); pon(19 - cx, +cy, +cy === 7 ? 'p' : 'm'); } });
  for (let cy = 2; cy <= 18; cy++) for (let cx = 5; cx <= 14; cx++) pon(cx, cy, 'b');
  // cuello en V
  [[7, 2], [8, 2], [11, 2], [12, 2], [8, 3], [11, 3], [9, 4], [10, 4]].forEach(([cx, cy]) => pon(cx, cy, 'k'));
  delete celdas['9,2']; delete celdas['10,2']; delete celdas['9,3']; delete celdas['10,3'];
  const c1 = x.c1, c2 = x.c2, c3 = x.c3 || x.c2;
  const cuerpo = (cx, cy) => {
    const bx = cx - 5, by = cy - 2;
    switch (x.p) {
      case 'franja': return by >= 6 && by <= 8 ? c2 : c1;
      case 'banda': return Math.abs(bx - by * 9 / 16) <= 1.1 ? c2 : c1;
      case 'rayas': return (bx < 5 ? bx : 9 - bx) % 2 === 0 ? c1 : c2;
      case 'rayas3': return [c1, c2, c3][bx % 3];
      case 'mitad': return bx < 5 ? c1 : c2;
      case 'aros': return Math.floor(by / 2) % 2 === 0 ? c1 : c2;
      case 'centro': return (x.ancho ? bx >= 3 && bx <= 6 : bx >= 4 && bx <= 5) ? c2 : c1;
      case 'diag': return bx + by * 10 / 17 < 9 ? c1 : c2;
      case 'cuadros': return (bx + by) % 2 === 0 ? c1 : c2;
      case 'v': { const d = Math.min(bx, 9 - bx); return (by === d || by === d + 1) && by <= 5 ? c2 : c1; }
      default: return c1;
    }
  };
  const manga = (cx, cy) => x.p === 'aros' ? (Math.floor((cy - 2) / 2) % 2 === 0 ? c1 : c2) : (x.m || (x.p === 'mitad' ? (cx < 10 ? c1 : c2) : c1));
  const detalle = x.k || (x.p === 'liso' ? c2 : (x.m && x.m !== c1 ? x.m : c2));
  // sombras: oscurece o aclara un color
  const tono = (hex, f) => {
    let h = String(hex).replace('#', ''); if (h.length === 3) h = h.split('').map(c => c + c).join('');
    if (!/^[0-9a-f]{6}$/i.test(h)) return hex;
    const n = [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16)).map(v => Math.round(f < 0 ? v * (1 + f) : v + (255 - v) * f));
    return '#' + n.map(v => Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0')).join('');
  };
  let r = '';
  for (let cy = 0; cy < H; cy++) for (let cx = 0; cx < W; cx++) {
    const t = celdas[cx + ',' + cy];
    let color = null;
    if (t === 'b') {
      color = cuerpo(cx, cy);
      if (cx >= 13 || cy === 18) color = tono(color, -0.22);          // sombra a la derecha y en el ruedo
      else if (cx === 5 && cy >= 8) color = tono(color, -0.15);       // pliegue bajo la manga izquierda
      else if (cx === 6 && cy >= 3 && cy <= 9) color = tono(color, 0.14); // brillo
    } else if (t === 'm') {
      color = manga(cx, cy);
      if (cy === 6 || cx >= 15) color = tono(color, -0.2);
    } else if (t === 'k' || t === 'p') color = detalle;
    else if ([[1,0],[-1,0],[0,1],[0,-1],[1,1],[-1,-1],[1,-1],[-1,1]].some(([a, b]) => celdas[(cx + a) + ',' + (cy + b)])) color = '#111';
    if (color) r += `<rect x="${cx}" y="${cy}" width="1.02" height="1.02" fill="${color}"/>`;
  }
  return `<svg class="kit-pix" viewBox="0 0 ${W} ${H}" shape-rendering="crispEdges" role="img">${r}</svg>`;
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
function caraHTML(j, cls = 'avatar') {
  // el redondel con el color y la sigla de su posición (si no se sabe la posición, una silueta)
  const pos = posDeTexto(j.posicion);
  return pos ? `<span class="${cls} pos-av" style="--c:${POS_COL[pos]}" title="${POS_NOM[pos]}">${POS_ABR[pos]}</span>` : `<span class="${cls}">${iniciales(j.nombre)}</span>`;
}
function cargarFotos() {}
/* ---------- Ficha extendida de un jugador: foto, datos y clubes anteriores ---------- */
async function bloqueJugadorExtra(nombre, destino) {
  const el = $(destino); if (!el) return;
  try {
    const x = await pedir('/api/jugador_extra?nombre=' + encodeURIComponent(nombre));
    if (!x.encontrado) { el.innerHTML = '<p class="nota-pie">No encontramos más información de este jugador.</p>'; return; }
    const fila = (t, v) => v ? `<div><small>${t}</small><b>${esc(v)}</b></div>` : '';
    el.innerHTML = `<section class="tarjeta pad"><h2 style="margin:0 0 12px;font-size:15px">Más datos</h2><div class="datos3">
        ${fila('Altura', x.altura)}${fila('Número', x.numero)}${fila('Posición', posES(x.posicion))}${fila('Club actual', x.equipo)}</div></section>
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
  if (trivia && v !== 'trivia') { clearInterval(trivia.reloj); trivia = null; }
  window.scrollTo(0, 0);
  const nav = { '': 'inicio', dia: 'inicio', calendario: 'calendario', favoritos: 'favoritos', jugador: '', ficha: '' };
  navAbajo(nav[v || ''] || '');
  if (!v) return vistaInicio();
  if (v === 'dia') return vistaInicio(a);
  if (v === 'partido') return vistaPartido(a);
  if (v === 'liga' && LIGAS[a]) return vistaLiga(a, b);
  if (v === 'calendario') return vistaCalendario();
  if (v === 'jugador') return vistaJugador(a, b);
  if (v === 'favoritos') return vistaFavoritos();
  if (v === 'envivo') return vistaEnVivo(a);
  if (v === 'trivia') return vistaTrivia();
  if (v === 'instalar') return vistaInstalar();
  if (v === 'club') return vistaClub(a);
  if (v === 'ficha') return vistaFicha(a);
  vistaInicio();
}
/* ---------- Avisos de gol en vivo ---------- */
// Modo guardado: 'off' | 'favs' (mis favoritos) | 'lpf' (toda la Liga Profesional) | 'todo' (favoritos + Liga Profesional)
const avisos = { modo: leer('tiki_avisos', 'off'), memo: leer('tiki_avisos_memo', {}), timer: null };
function idsAvisos() { return leer('tiki_favs', []).map(f => String(f.id)); }
function partidoVigilado(p) {
  const favs = idsAvisos();
  const esFav = favs.includes(String(p.local.id)) || favs.includes(String(p.visitante.id));
  if (avisos.modo === 'favs') return esFav;
  if (avisos.modo === 'lpf') return p.liga === 'LPF';
  if (avisos.modo === 'todo') return esFav || p.liga === 'LPF';
  return false;
}
async function revisarGoles() {
  clearTimeout(avisos.timer);
  if (avisos.modo === 'off') return;
  let proximo = 5 * 60 * 1000;  // sin partidos en juego: cada 5 minutos
  try {
    const d = await pedir('/api/hoy?fecha=' + hoyISO());
    const partidos = d.grupos.flatMap(g => g.partidos).filter(partidoVigilado);
    const ahora = Date.now();
    for (const p of partidos) {
      const antes = avisos.memo[p.id];
      const gl = p.gl ?? 0, gv = p.gv ?? 0;
      if (antes && (p.vivo || p.terminado)) {
        if (gl > antes.gl) avisoGol(p, p.local, gl, gv);
        if (gv > antes.gv) avisoGol(p, p.visitante, gl, gv);
        if (p.terminado && !antes.fin) mostrarAviso(p, '🏁 ¡Final del partido!', `${p.local.nombre} ${gl} - ${gv} ${p.visitante.nombre}`);
      }
      avisos.memo[p.id] = { gl, gv, fin: !!p.terminado, t: ahora };
      const inicio = new Date(p.fecha + 'T' + p.hora + ':00').getTime();
      if (p.vivo || (inicio - ahora < 15 * 60 * 1000 && !p.terminado)) proximo = 45 * 1000;  // en juego: cada 45 segundos
    }
    Object.keys(avisos.memo).forEach(k => { if (ahora - avisos.memo[k].t > 2 * 86400000) delete avisos.memo[k]; });
    guardar('tiki_avisos_memo', avisos.memo);
  } catch (e) { proximo = 2 * 60 * 1000; }
  avisos.timer = setTimeout(revisarGoles, proximo);
}
async function avisoGol(p, equipo, gl, gv) {
  let quien = p.minuto ? ` · ${p.minuto}'` : '';
  try {
    // quién lo hizo: se busca en la ficha del partido (si todavía no está cargado, sale sin nombre)
    const ctl = new AbortController(); const corte = setTimeout(() => ctl.abort(), 4000);  // si tarda, avisa igual sin el nombre
    const r = await fetch('/api/partido?id=' + p.id, { signal: ctl.signal }); clearTimeout(corte);
    const d = r.ok ? await r.json() : {};
    const g = (d.goles || []).filter(x => String(x.equipo) === String(equipo.id)).pop();
    if (g && g.jugador) quien = ` · ${g.jugador}${g.tipo === 'PENALTY' ? ' (penal)' : g.tipo === 'OWN' ? ' (en contra)' : ''}${g.minuto ? ' ' + g.minuto + "'" : ''}`;
  } catch (e) {}
  mostrarAviso(p, `⚽ ¡GOL de ${equipo.nombre}!`, `${p.local.nombre} ${gl} - ${gv} ${p.visitante.nombre}${quien}`);
}
function mostrarAviso(p, titulo, texto) {
  // 1) cartel dentro de la app, como una notificación
  const n = document.createElement('a');
  n.className = 'aviso-gol'; n.href = '#/partido/' + p.id;
  n.innerHTML = `<img src="/logo.png" alt=""><span style="flex:1;min-width:0"><small>TIKI DATA · ahora</small><b>${esc(titulo)}</b><span>${esc(texto)}</span></span>`;
  n.onclick = () => n.remove();
  document.body.appendChild(n);
  requestAnimationFrame(() => n.classList.add('on'));
  setTimeout(() => { n.classList.remove('on'); setTimeout(() => n.remove(), 400); }, 7000);
  if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
  // 2) notificación del celular (si dio permiso)
  if ('Notification' in window && Notification.permission === 'granted' && navigator.serviceWorker) {
    navigator.serviceWorker.ready.then(reg => reg.showNotification(titulo, {
      body: texto, icon: '/icono-192.png', badge: '/icono-192.png', tag: 'gol-' + p.id + '-' + titulo, data: { url: '/#/partido/' + p.id } })).catch(() => {});
  }
}
async function cambiarAvisos(modo) {
  if (modo !== 'off' && 'Notification' in window && Notification.permission === 'default') {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  avisos.modo = modo; guardar('tiki_avisos', modo);
  revisarGoles();
}
function bloqueAvisos() {
  const opciones = [['off', 'Apagados'], ['favs', 'Mis favoritos'], ['lpf', 'Liga Profesional'], ['todo', 'Favoritos + Liga']];
  const permiso = !('Notification' in window) ? 'Tu navegador no permite notificaciones: los avisos aparecen dentro de la app.'
    : Notification.permission === 'denied' ? 'Bloqueaste las notificaciones en el navegador: los avisos aparecen solo dentro de la app.'
    : 'Te avisamos cada gol mientras tengas Tiki Data abierta (o en segundo plano).';
  return `<section class="tarjeta pad"><b style="font-size:15px">🔔 Avisos de gol en vivo</b>
    <div class="chips" style="margin-top:10px">${opciones.map(([k, n]) => `<button class="chip ${avisos.modo === k ? 'on' : ''}" data-avisos="${k}">${n}</button>`).join('')}</div>
    <p class="nota-pie" style="margin:8px 0 0">${permiso}</p>
    <button class="link" id="probarAviso" style="margin-top:6px">Probar cómo se ve un aviso</button></section>`;
}
function activarBloqueAvisos(redibujar) {
  document.querySelectorAll('[data-avisos]').forEach(b => b.onclick = async () => { await cambiarAvisos(b.dataset.avisos); redibujar(); });
  const pr = $('probarAviso');
  if (pr) pr.onclick = () => mostrarAviso({ id: 'ejemplo' }, '⚽ ¡GOL de Tiki Data!', 'Así te avisamos cada gol de tus equipos');
}
/* ---------- Instalar como app (PWA) ---------- */
let pedidoInstalar = null;
const esIphone = /iphone|ipad|ipod/i.test(navigator.userAgent);
const yaInstalada = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
function puedeInstalar() { return !yaInstalada() && (pedidoInstalar || esIphone); }
function activarInstalar() {
  const b = $('btnInstalar'); if (!b) return;
  b.hidden = !puedeInstalar();
  b.onclick = async () => {
    if (pedidoInstalar) { pedidoInstalar.prompt(); await pedidoInstalar.userChoice; pedidoInstalar = null; b.hidden = true; return; }
    if (esIphone) alertaIphone();
  };
}
function alertaIphone() {
  const d = document.createElement('div');
  d.className = 'tarjeta pad';
  d.style.cssText = 'position:fixed;left:12px;right:12px;bottom:90px;z-index:50;max-width:520px;margin:0 auto;box-shadow:0 10px 40px rgba(0,0,0,.6)';
  d.innerHTML = '<b style="font-size:15px">Instalar Tiki Data en el iPhone</b><ol style="margin:10px 0 12px;padding-left:20px;font-size:14px;line-height:1.6;color:#C7D2E0"><li>Abrí la página en <b>Safari</b>.</li><li>Tocá el botón <b>Compartir</b> (el cuadrado con la flecha para arriba).</li><li>Elegí <b>Agregar a pantalla de inicio</b>.</li></ol><button class="boton" style="width:100%">Entendido</button>';
  d.querySelector('button').onclick = () => d.remove();
  document.body.appendChild(d);
}
function vistaInstalar() {
  const android = /android/i.test(navigator.userAgent);
  const pasos = yaInstalada() ? '<div class="tarjeta aviso" style="text-align:center">✅ Ya tenés Tiki Data instalada en este celular.</div>'
    : pedidoInstalar ? '<button class="boton" id="instalarYa" style="width:100%;font-size:17px;padding:16px">📲 Instalar Tiki Data</button>'
    : esIphone ? `<section class="tarjeta pad"><b style="font-size:15px">En el iPhone es así:</b><ol style="margin:10px 0 0;padding-left:20px;font-size:15px;line-height:1.7;color:#C7D2E0">
        <li>Abrí este link en <b>Safari</b>.</li><li>Tocá el botón <b>Compartir</b> (el cuadrado con la flecha para arriba).</li><li>Elegí <b>Agregar a pantalla de inicio</b> y después <b>Agregar</b>.</li></ol></section>`
    : `<section class="tarjeta pad"><b style="font-size:15px">${android ? 'En Android' : 'En la compu'} es así:</b><ol style="margin:10px 0 0;padding-left:20px;font-size:15px;line-height:1.7;color:#C7D2E0">
        <li>Abrí este link en <b>Chrome</b>.</li><li>Tocá los <b>tres puntitos</b> (⋮) arriba a la derecha.</li><li>Elegí <b>Instalar aplicación</b> o <b>Agregar a la pantalla principal</b>.</li></ol></section>`;
  $('vista').innerHTML = `<main style="align-items:center;text-align:center;padding-top:26px">
    <img src="/icono-512.png" alt="Tiki Data" style="width:150px;height:150px;border-radius:34px;box-shadow:0 10px 40px rgba(47,107,219,.35)">
    <b class="titulo" style="font-size:28px">TIKI<span style="color:#4F8FF7">DATA</span></b>
    <p style="margin:-4px 0 6px;color:#C7D2E0;font-size:15px;line-height:1.5">Resultados en vivo, avisos de gol, tablas y noticias.<br>Gratis y sin ocupar lugar en el celular.</p>
    <div style="width:100%;max-width:420px;display:flex;flex-direction:column;gap:12px;text-align:left">${pasos}
      <a class="boton sec" href="#/" style="text-align:center">Entrar sin instalar</a></div></main>`;
  const b = $('instalarYa');
  if (b) b.onclick = async () => { pedidoInstalar.prompt(); await pedidoInstalar.userChoice; pedidoInstalar = null; vistaInstalar(); };
}
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); pedidoInstalar = e; activarInstalar(); if (location.hash.startsWith('#/instalar')) vistaInstalar(); });
window.addEventListener('appinstalled', () => { pedidoInstalar = null; activarInstalar(); });
if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
armarMenu();
activarInstalar();
revisarGoles();
window.addEventListener('hashchange', ruta);
// Contador de visitas: cuenta cada pantalla que se mira (inicio, partido, club, etc.)
function pantallaActual() { const h = (location.hash || '#/').replace(/^#/, ''); return '/' + (h.split('/')[1] || 'inicio'); }
function contarVisita(intento = 0) {
  const pantalla = pantallaActual();
  if (window.goatcounter && window.goatcounter.count) window.goatcounter.count({ path: pantalla, title: document.title, event: false });
  else if (intento < 20) setTimeout(() => contarVisitaGC(intento + 1, pantalla), 500);
  contarUmami(0, pantalla);
}
function contarVisitaGC(intento, pantalla) {
  if (window.goatcounter && window.goatcounter.count) window.goatcounter.count({ path: pantalla, title: document.title, event: false });
  else if (intento < 20) setTimeout(() => contarVisitaGC(intento + 1, pantalla), 500);
}
// Umami: cada pantalla de la app se cuenta como una página (inicio, partido, liga, calendario…)
function contarUmami(intento, pantalla) {
  if (window.umami && window.umami.track) { try { window.umami.track(props => ({ ...props, url: pantalla, title: 'Tiki Data ' + pantalla })); } catch (e) {} }
  else if (intento < 20) setTimeout(() => contarUmami(intento + 1, pantalla), 500);
}
window.addEventListener('hashchange', () => contarVisita());
window.addEventListener('load', () => contarVisita());
ruta();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# El servidor: responde a la página
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# App instalable en el celular (PWA): manifiesto, íconos y "service worker"
# (el service worker guarda la página para que abra rápido y funcione sin señal)
# ---------------------------------------------------------------------------
MANIFIESTO = {
    "name": "Tiki Data — El fútbol en datos", "short_name": "Tiki Data", "lang": "es-AR",
    "description": "Resultados, tablas, noticias y clubes. El fútbol en datos.",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#070B14", "theme_color": "#05070D",
    "icons": [
        {"src": "/icono-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icono-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icono-mask.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
SERVICE_WORKER = r"""
const CACHE = 'tiki-v4';
const BASE = ['/', '/logo.png', '/icono-192.png', '/manifest.webmanifest'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(BASE)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
// Al tocar un aviso de gol: abre (o trae al frente) Tiki Data en ese partido
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) { if ('focus' in c) { c.navigate(url).catch(() => {}); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
// La página y los íconos: se abren al instante desde el celular y se actualizan por detrás.
// Los datos (/api/...): primero internet; si no hay señal, lo último guardado.
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  // La página: primero internet (así siempre está la última versión); si tarda más de 3 segundos, la guardada.
  if (r.mode === 'navigate' || u.pathname === '/') {
    const red = fetch(r).then(resp => { if (resp.ok) { const c2 = resp.clone(); caches.open(CACHE).then(c => c.put('/', c2)); } return resp; });
    e.respondWith(Promise.race([red, new Promise(ok => setTimeout(ok, 3000))]).then(x => x || caches.match('/').then(g => g || red)).catch(() => caches.match('/')));
    return;
  }
  if (!u.pathname.startsWith('/api/') && u.pathname !== '/sw.js') {
    e.respondWith(caches.open(CACHE).then(c => c.match(r).then(guardada => {
      const nueva = fetch(r).then(resp => { if (resp.ok) c.put(r, resp.clone()); return resp; }).catch(() => guardada || caches.match('/'));
      return guardada || nueva;
    })));
    return;
  }
  e.respondWith(fetch(r).then(resp => {
    if (resp.ok) { const copia = resp.clone(); caches.open(CACHE).then(c => c.put(r, copia)); }
    return resp;
  }).catch(() => caches.match(r).then(x => x || caches.match('/'))));
});
"""
# Imagen que aparece al compartir el link (WhatsApp, Instagram, Facebook)
IMAGEN_COMPARTIR = "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6asJ3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoyn3H+ub8P5UedJ/e/SpLiaTzm+b9Kelha3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoJ/qZPw/nTKnSaTyZPm9O3vUfnSf3v0puwtRlFP86T+9+lHnSf3v0paD1GUU/wA6T+9+lHnSf3v0o0DUZRT/ADpP736UedJ/e/SjQNQuP9c34fyplT3E0nnN836VH50n979KbtcSvYZRT/Ok/vfpR50n979KWg9RlFP86T+9+lHnSf3v0o0DUZT0/wBTJ+H86POk/vfpUiTSeTJ83p296asJ3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoyn3H+ub8P5UedJ/e/SpLiaTzm+b9Kelha3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoJ/qZPw/nTKnSaTyZPm9O3vUfnSf3v0puwtRlFP86T+9+lHnSf3v0paD1GUU/wA6T+9+lHnSf3v0o0DUZRT/ADpP736UedJ/e/SjQNQuP9c34fyplT3E0nnN836VH50n979KbtcSvYZRT/Ok/vfpR50n979KWg9RlFP86T+9+lHnSf3v0o0DUZT0/wBTJ+H86POk/vfpUiTSeTJ83p296asJ3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoyn3H+ub8P5UedJ/e/SpLiaTzm+b9Kelha3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KPOk/vfpRoGoJ/qZPw/nTKnSaTyZPm9O3vUfnSf3v0puwtRlFP86T+9+lHnSf3v0paD1GUU/wA6T+9+lHnSf3v0o0DUZRT/ADpP736UedJ/e/SjQNQuP9c34fyplT3E0nnN836VH50n979KbtcSvYZRT/Ok/vfpR50n979KWg9RlFP86T+9+lHnSf3v0o0DUZT0/wBTJ+H86POk/vfpUiTSeTJ83p296asJ3IKKf50n979KPOk/vfpS0HqMop/nSf3v0o86T+9+lGgajKKf50n979KP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"
ICONOS_RUTAS = {"/icono-192.png": "192", "/icono-512.png": "512", "/icono-mask.png": "mask", "/apple-touch-icon.png": "apple"}
ICONOS = {"192": "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", "512": "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", "mask": "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", "apple": "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"}


class Manejador(BaseHTTPRequestHandler):
    def responder(self, codigo, tipo, cuerpo):
        datos = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        comprimido = False
        if len(datos) > 1400 and ("json" in tipo or "text" in tipo or "javascript" in tipo) and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            import gzip
            datos = gzip.compress(datos, 6)  # viaja hasta 4 veces más liviano
            comprimido = True
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        if comprimido:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if tipo.startswith("image/"):
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def json(self, funcion):
        inicio = time.time()
        try:
            datos = json.dumps(en_castellano(funcion()))
            _medir(urlparse(self.path).path, time.time() - inicio)
            self.responder(200, "application/json", datos)
        except Exception as e:
            self.responder(502, "application/json", json.dumps({"error": str(e)}))

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        liga = q.get("liga", "")
        if url.path == "/":
            hl_vis = [k for k in HL_LIGAS if k not in _hl_no_encontradas] if HL_KEY else []
            visibles = {**{k: list(HL_LIGAS[k]) for k in hl_vis}, **{k: list(v) for k, v in LIGAS.items()}}
            host = re.sub(r"[^A-Za-z0-9.:-]", "", self.headers.get("Host") or "tikidata.onrender.com")
            base = ("http://" if host.startswith(("localhost", "127.")) else "https://") + host
            pagina = PAGINA.replace("__BASE__", base).replace("__LIGAS__", json.dumps(visibles, ensure_ascii=False)).replace("__HL__", json.dumps(hl_vis)).replace("__CONTADOR__", (
                f'<script data-goatcounter="https://{GOATCOUNTER}.goatcounter.com/count" data-goatcounter-settings=\'{{"no_onload": true}}\' async src="https://gc.zgo.at/count.js"></script>'
                if GOATCOUNTER else "") + (
                f'<script defer src="https://cloud.umami.is/script.js" data-website-id="{UMAMI_ID}" data-auto-track="false"></script>'
                if UMAMI_ID else ""))
            self.responder(200, "text/html; charset=utf-8", pagina)
        elif url.path in ("/instalar", "/descargar", "/app"):
            self.send_response(302)
            self.send_header("Location", "/#/instalar")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == "/compartir.jpg":
            self.responder(200, "image/jpeg", base64.b64decode(IMAGEN_COMPARTIR))
        elif url.path in ICONOS_RUTAS:
            self.responder(200, "image/png", base64.b64decode(ICONOS[ICONOS_RUTAS[url.path]]))
        elif url.path == "/manifest.webmanifest":
            self.responder(200, "application/manifest+json", json.dumps(MANIFIESTO, ensure_ascii=False))
        elif url.path == "/sw.js":
            self.responder(200, "application/javascript; charset=utf-8", SERVICE_WORKER)
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
            self.json(lambda: hoy_rapido(fecha))
        elif url.path == "/api/calendario_mes" and re.fullmatch(r"\d{4}", q.get("anio", "")) and re.fullmatch(r"\d{1,2}", q.get("mes", "")):
            self.json(lambda: datos_calendario_mes(int(q["anio"]), int(q["mes"])))
        elif url.path == "/api/calendario":
            self.json(lambda: calendario_rapido(liga))
        elif url.path == "/api/partido" and re.fullmatch(r"h?\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_partido(q["id"]))
        elif url.path == "/api/tabla":
            self.json(lambda: datos_tabla(liga))
        elif url.path == "/api/goleadores":
            self.json(lambda: datos_goleadores(liga))
        elif url.path == "/api/equipos":
            self.json(lambda: datos_equipos(liga))
        elif url.path == "/api/envivo":
            self.json(datos_en_vivo)
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
        elif url.path == "/api/escudos" and 0 < len(q.get("nombres", "")) <= 1200:
            self.json(lambda: datos_escudos(q["nombres"].split("|")))
        elif url.path == "/api/resumen_goles" and re.fullmatch(r"(h?\d{1,12},?){1,30}", q.get("ids", "")):
            self.json(lambda: datos_resumen_goles([x for x in q["ids"].split(",") if x]))
        elif url.path == "/api/hl_muestra" and re.fullmatch(r"h\d{1,12}", q.get("id", "")):
            self.json(lambda: hl_muestra(q["id"][1:]))
        elif url.path == "/api/jugadores_partido" and re.fullmatch(r"h\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_jugadores_partido(q["id"][1:]))
        elif url.path == "/api/estadisticas" and re.fullmatch(r"h\d{1,12}", q.get("id", "")):
            self.json(lambda: datos_estadisticas_hl(q["id"][1:]))
        elif url.path == "/api/diagnostico":
            self.json(datos_diagnostico)
        elif url.path == "/api/hl_buscar":
            self.json(hl_buscar_ligas)
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
    threading.Thread(target=precalentar, daemon=True).start()
    if HL_KEY:
        threading.Thread(target=goleadores_al_arrancar, daemon=True).start()
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
