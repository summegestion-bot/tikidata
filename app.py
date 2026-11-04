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
# Si alguno deja de funcionar, se saltea solo. Se pueden agregar más.
NOTICIAS = [
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("MARCA", "https://e00-marca.uecdn.es/rss/futbol/futbol-internacional.xml"),
    ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/portada/"),
    ("SPORT", "https://www.sport.es/es/rss/last-news/news.xml"),
    # Titulares de medios argentinos (Olé, TyC, ESPN, etc.) a través de Google Noticias
    ("", "https://news.google.com/rss/search?q=f%C3%BAtbol+argentino&hl=es-419&gl=AR&ceid=AR:es-419"),
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
    datos = api(f"/competitions/{liga}/matches", ttl=1800)
    comp = datos.get("competition") or {}
    lista = [partido(p, liga) for p in datos.get("matches", [])]
    for p in lista:
        p["ligaEmblema"] = p["ligaEmblema"] or comp.get("emblem", "")
    return {"liga": liga, "nombre": LIGAS[liga][0], "emblema": comp.get("emblem", ""), "partidos": lista}


def datos_partido(id_partido):
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
    datos = api(f"/competitions/{liga}/teams", ttl=86400)
    lista = [equipo(t) for t in datos.get("teams", [])]
    lista.sort(key=lambda t: t["nombre"])
    return {"liga": liga, "equipos": lista}


def datos_proximos(id_equipo):
    datos = api(f"/teams/{id_equipo}/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED&limit=3", ttl=900)
    return {"partidos": [partido(p) for p in datos.get("matches", [])]}


_noticias = {"t": 0, "lista": []}


def imagen_de(item):
    for hijo in item:
        etiqueta = hijo.tag.lower()
        if (etiqueta.endswith("}content") or etiqueta.endswith("}thumbnail")) and hijo.get("url"):
            return hijo.get("url")
        if etiqueta == "enclosure" and "image" in (hijo.get("type") or "image"):
            if hijo.get("url"):
                return hijo.get("url")
        for nieto in hijo:
            if nieto.tag.lower().endswith("}content") and nieto.get("url"):
                return nieto.get("url")
    desc = (item.findtext("description") or "") + (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
    m = re.search(r'<img[^>]+src="([^"]+)"', desc)
    return m.group(1) if m else ""


def datos_noticias():
    if time.time() - _noticias["t"] < 900 and _noticias["lista"]:
        return {"noticias": _noticias["lista"]}
    lista, vistos = [], set()
    for fuente, url in NOTICIAS:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": NAVEGADOR})
            raiz = ET.fromstring(r.content)
        except Exception:
            continue
        for item in raiz.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            enlace = (item.findtext("link") or "").strip()
            if not titulo or not enlace or enlace in vistos:
                continue
            vistos.add(enlace)
            try:
                cuando = parsedate_to_datetime(item.findtext("pubDate")).astimezone(ARG)
            except Exception:
                cuando = None
            origen = fuente or (item.findtext("source") or "NOTICIAS").strip().upper()
            if not fuente and " - " in titulo:
                titulo = titulo.rsplit(" - ", 1)[0]
            lista.append({"fuente": origen, "titulo": titulo, "enlace": enlace,
                          "imagen": imagen_de(item),
                          "orden": cuando.timestamp() if cuando else 0,
                          "cuando": cuando.strftime("%d/%m %H:%M") if cuando else ""})
    lista.sort(key=lambda n: n["orden"], reverse=True)
    # Mezclar portales: máximo 4 noticias de cada uno, y arriba una con foto
    por_fuente, elegidas = {}, []
    for n in lista:
        if por_fuente.get(n["fuente"], 0) < 4:
            por_fuente[n["fuente"]] = por_fuente.get(n["fuente"], 0) + 1
            elegidas.append(n)
    con_foto = [n for n in elegidas if n["imagen"]]
    if con_foto:
        elegidas.remove(con_foto[0])
        elegidas.insert(0, con_foto[0])
    _noticias.update(t=time.time(), lista=elegidas[:13])
    return {"noticias": _noticias["lista"]}


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
.pestanas { display:grid; grid-auto-flow:column; grid-auto-columns:1fr; background:var(--negro); border-bottom:1px solid var(--linea); }
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
    <p>© 2026 Tiki Data · Datos de football-data.org</p>
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
const COLOR = { CL:'#8DB8FF', PL:'#C084FC', PD:'#FB923C', SA:'#2DD4BF', BL1:'#F87171', FL1:'#FACC15', PPL:'#22C55E', DED:'#F97316', ELC:'#A3A3A3', BSA:'#4ADE80', WC:'#E879F9' };
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
  return t && t.escudo ? `<img class="${cls}" src="${esc(t.escudo)}" alt="" loading="lazy">`
    : `<span class="${cls} ph">${esc(((t && (t.sigla || t.nombre)) || '?').slice(0,3))}</span>`;
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
  const grupos = [['EUROPA', ['CL','PL','PD','SA','BL1','FL1','PPL','DED','ELC']], ['AMÉRICA', ['BSA']], ['SELECCIONES', ['WC']]];
  let h = `<div class="grupo">
    <a class="item" href="#/">${svg(ICON.inicio,20)}Inicio</a>
    <a class="item" href="#/calendario">${svg(ICON.cal,20)}Calendario</a>
    <a class="item" href="#/jugadores">${svg(ICON.jug,20)}Goleadores y comparador</a>
    <a class="item" href="#/favoritos">${svg(ICON.estrella,20)}Mis favoritos</a></div>`;
  grupos.forEach(([n, cods]) => {
    h += `<div class="grupo"><small>${n}</small>` + cods.map(c => `<a class="item" href="#/liga/${c}"><span class="punto" style="background:${COLOR[c]}"></span>${esc(LIGAS[c][0])}</a>`).join('');
    if (n === 'AMÉRICA') h += `<span class="item off"><span class="punto" style="background:#475569"></span>Liga Profesional Argentina<em>PRÓXIMAMENTE</em></span><span class="item off"><span class="punto" style="background:#475569"></span>Copa Libertadores<em>PRÓXIMAMENTE</em></span>`;
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
async function cargarNoticias() {
  const el = $('noticias'); if (!el) return;
  try {
    const d = await pedir('/api/noticias');
    if (!d.noticias.length) { el.innerHTML = ''; return; }
    const [a, ...resto] = d.noticias;
    const img = (n) => n.imagen ? `<img src="${esc(n.imagen)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<img src="/logo.png" alt="" style="object-fit:contain;padding:14px;opacity:.5" onerror="this.remove()">`;
    el.innerHTML = `<section style="display:flex;flex-direction:column;gap:12px">
      <div class="seccion"><h2>Noticias</h2></div>
      <a class="hero" href="${esc(a.enlace)}" target="_blank" rel="noopener">${img(a)}
        <div class="capa"><span class="fuente">${esc(a.fuente)}</span><div class="tit">${esc(a.titulo)}</div><small>${esc(a.cuando)} · Leer en ${esc(a.fuente)}</small></div></a>
      <div class="grilla">${resto.slice(0, 8).map(n => `<a class="nota" href="${esc(n.enlace)}" target="_blank" rel="noopener">
        <div class="img">${img(n)}<span class="fuente">${esc(n.fuente)}</span></div>
        <div class="txt">${esc(n.titulo)}<small>${esc(n.cuando)}</small></div></a>`).join('')}</div></section>`;
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
  const pest = ['Resumen', 'Goles', 'Historial'];
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
    () => `<a class="boton yt" href="${yt}" target="_blank" rel="noopener">${svg('M8 5v14l11-7z',18,'fill="currentColor" stroke="none"')}Ver resumen en YouTube</a>
      <section class="tarjeta pad"><h2 style="margin:0 0 4px;font-size:15px">Información</h2>
        <div class="dato"><span>Fecha</span><span>${esc(fechaTxt)}</span></div>
        ${p.medio && p.medio[0] !== null && p.medio[0] !== undefined ? `<div class="dato"><span>Primer tiempo</span><span>${p.medio[0]} - ${p.medio[1]}</span></div>` : ''}
        ${p.estadio ? `<div class="dato"><span>Estadio</span><span>${esc(p.estadio)}</span></div>` : ''}
        ${p.arbitro ? `<div class="dato"><span>Árbitro</span><span>${esc(p.arbitro)}</span></div>` : ''}
      </section>
      <p class="nota-pie">El minuto a minuto, las formaciones y las estadísticas del partido no vienen en el plan gratuito de la API. Se pueden sumar con una API más completa.</p>`,
    () => p.goles.length ? `<section class="tarjeta pad">${p.goles.map(g => `<div class="dato"><span style="color:var(--texto);font-weight:700">${g.minuto ? g.minuto + "'" : ''}</span><span style="flex:1">${esc(g.jugador)}${g.tipo === 'OWN' ? ' (en contra)' : g.tipo === 'PENALTY' ? ' (penal)' : ''}</span><span>${esc(g.equipo === p.local.id ? p.local.nombre : p.visitante.nombre)}</span></div>`).join('')}</section>`
      : `<div class="tarjeta aviso">${jugado ? 'El plan gratuito de la API no trae quién hizo los goles de este partido.' : 'Todavía no se jugó.'}</div>`,
    () => {
      const h = p.historial;
      if (!h || !h.total) return '<div class="tarjeta aviso">No hay historial entre estos equipos.</div>';
      return `<section class="tarjeta pad"><h2 style="margin:0 0 10px;font-size:15px">Últimos ${h.total} enfrentamientos</h2>
        <div class="datos3"><div><small>Ganó ${esc(p.local.nombre)}</small><b>${h.local ?? 0}</b></div><div><small>Empates</small><b>${h.empates ?? 0}</b></div><div><small>Ganó ${esc(p.visitante.nombre)}</small><b>${h.visitante ?? 0}</b></div></div></section>
        <section class="tarjeta">${h.partidos.map(x => filaPartido(x, true)).join('')}</section>`;
    }
  ];
  const mostrar = (i) => { $('panel').innerHTML = paneles[i](); document.querySelectorAll('.pestanas button').forEach(b => b.classList.toggle('on', +b.dataset.p === i)); };
  document.querySelectorAll('.pestanas button').forEach(b => b.onclick = () => mostrar(+b.dataset.p));
  mostrar(0);
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
  const cods = Object.keys(LIGAS);
  await Promise.all(cods.map(async c => { if (!cal.datos[c]) { try { cal.datos[c] = (await pedir('/api/calendario?liga=' + c)).partidos; } catch (e) { cal.datos[c] = []; } } }));
  dibujarCalendario();
}
function dibujarCalendario() {
  const activas = cal.liga === 'ALL' ? Object.keys(LIGAS) : [cal.liga];
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
  if (!d.goleadores.length) return '<div class="tarjeta aviso">Todavía no hay goleadores.</div>';
  return `<section class="tarjeta">${d.goleadores.map((j, i) => `<a class="jug" href="#/jugador/${cod}/${j.id}">
    <span class="pos ${i < 3 ? 'top' : ''}">${i + 1}</span><span class="avatar">${iniciales(j.nombre)}</span>
    <span style="min-width:0"><b class="nom" style="display:block;font-size:14px">${esc(j.nombre)}</b><small style="color:var(--suave);display:flex;align-items:center;gap:6px">${escudo(j.equipo)}${esc(j.equipo.nombre)}</small></span>
    <span style="text-align:right"><span class="goles">${j.goles}</span><br><small style="color:var(--suave)">${j.asistencias} asist.</small></span></a>`).join('')}</section>`;
}
let ligaJug = 'PD';
async function vistaJugadores() {
  $('vista').innerHTML = volver('Goleadores') + `<main><div class="chips">${Object.keys(LIGAS).map(c => `<button class="chip ${c === ligaJug ? 'on' : ''}" data-l="${c}">${esc(LIGAS[c][0])}</button>`).join('')}</div>
    <a class="boton sec" href="#/comparar">Comparar dos jugadores</a><div id="lista"><div class="cargando">Cargando…</div></div></main>`;
  document.querySelectorAll('[data-l]').forEach(b => b.onclick = () => { ligaJug = b.dataset.l; vistaJugadores(); });
  try { $('lista').innerHTML = listaGoleadores(await goleadores(ligaJug), ligaJug); } catch (e) { $('lista').innerHTML = error(e); }
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
      <p class="nota-pie">La foto, el valor de mercado, los atributos y el historial de clubes necesitan una API más completa. Por ahora se muestran los datos de goleadores.</p></main>`;
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
      ${[a, b].map((x, i) => `<div class="tarjeta pad" style="text-align:center;border-top:3px solid ${i ? '#FF9F43' : 'var(--plata)'};display:flex;flex-direction:column;align-items:center;gap:6px"><span class="avatar" style="width:56px;height:56px;border-radius:28px;font-size:17px">${iniciales(x.nombre)}</span><b style="font-size:14px">${esc(x.nombre)}</b><small style="color:var(--suave);display:flex;gap:6px;align-items:center">${escudo(x.equipo)}${esc(x.equipo.nombre)}</small></div>`).join('')}</div>
      <section class="tarjeta pad">${filas}</section>`;
  };
  $('vista').innerHTML = volver('Comparar jugadores', '#/jugadores') + `<main>
    <div class="chips">${Object.keys(LIGAS).map(c => `<a class="chip ${c === cod ? 'on' : ''}" href="#/comparar/${c}">${esc(LIGAS[c][0])}</a>`).join('')}</div>
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
    <div class="chips">${Object.keys(LIGAS).filter(c => c !== 'CL' && c !== 'WC').map(c => `<button class="chip ${c === ligaFav ? 'on' : ''}" data-l="${c}">${esc(LIGAS[c][0])}</button>`).join('')}</div>
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

/* ---------- Rutas ---------- */
function ruta() {
  const partes = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const [v, a, b] = partes;
  window.scrollTo(0, 0);
  const nav = { '': 'inicio', dia: 'inicio', calendario: 'calendario', favoritos: 'favoritos', jugadores: 'jugadores', jugador: 'jugadores', comparar: 'jugadores' };
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
            pagina = PAGINA.replace("__LIGAS__", json.dumps({k: list(v) for k, v in LIGAS.items()}, ensure_ascii=False))
            self.responder(200, "text/html; charset=utf-8", pagina)
        elif url.path == "/logo.png":
            ruta = os.path.join(CARPETA, "tikidata_escudo.png")
            if os.path.exists(ruta):
                with open(ruta, "rb") as f:
                    self.responder(200, "image/png", f.read())
            else:
                self.responder(404, "text/plain", "Sin logo")
        elif url.path.startswith("/api/") and liga and liga not in LIGAS:
            self.responder(400, "application/json", json.dumps({"error": "Liga no disponible"}))
        elif url.path == "/api/hoy":
            fecha = q.get("fecha") or datetime.now(ARG).strftime("%Y-%m-%d")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
                self.responder(400, "application/json", json.dumps({"error": "Fecha inválida"}))
                return
            self.json(lambda: datos_hoy(fecha))
        elif url.path == "/api/calendario":
            self.json(lambda: datos_calendario(liga))
        elif url.path == "/api/partido" and q.get("id", "").isdigit():
            self.json(lambda: datos_partido(q["id"]))
        elif url.path == "/api/tabla":
            self.json(lambda: datos_tabla(liga))
        elif url.path == "/api/goleadores":
            self.json(lambda: datos_goleadores(liga))
        elif url.path == "/api/equipos":
            self.json(lambda: datos_equipos(liga))
        elif url.path == "/api/proximos" and q.get("equipo", "").isdigit():
            self.json(lambda: datos_proximos(q["equipo"]))
        elif url.path == "/api/noticias":
            self.json(datos_noticias)
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
