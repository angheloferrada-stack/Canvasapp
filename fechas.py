"""Escanea Canvas (tareas, anuncios, módulos con PDFs/Word y páginas) y Gmail,
usa Gemini para sacar las fechas importantes y genera docs/fechas.ics."""
import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from docx import Document
from pypdf import PdfReader

INICIO = time.time()
TIEMPO_MAX = 4 * 3600  # la corrida se detiene sola a las 4 horas; GitHub la corta a las 6
ZONA = ZoneInfo("America/Santiago")
HOY = dt.datetime.now(ZONA).date()
CANVAS_URL = os.environ["CANVAS_URL"].rstrip("/")
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODELO = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GMAIL_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH = os.environ.get("GMAIL_REFRESH_TOKEN", "")
PAUSA_GEMINI = 8  # segundos entre consultas, para no pasar el límite por minuto del plan gratis
PAGINAS_MAX = 80  # por PDF, para que la lectura no tarde demasiado
SALIDA = pathlib.Path("docs/fechas.ics")
CACHE = pathlib.Path("docs/cache.json")

# 1) Por nombre: solo se consideran archivos que sugieran fechas
PALABRAS_FECHAS = re.compile(
    r"cronograma|programa|calendario|evaluaci|prueba|control|certam|solemne|examen|"
    r"entrega|plazo|fecha|temario|informe",
    re.IGNORECASE,
)
# 2) Por nombre: se descartan pautas y correcciones sin abrirlas
DESCARTAR_NOMBRE = re.compile(r"pauta|correcci|soluci|respuesta|resuelt", re.IGNORECASE)
# 3) Por contenido (inicio del documento): pautas y enunciados de controles
DESCARTAR_INICIO = re.compile(r"pauta|correcci|soluci|respuestas|puntaje", re.IGNORECASE)
PALABRAS_EXAMEN = re.compile(r"control|certamen|solemne|prueba|examen", re.IGNORECASE)
PALABRAS_PREGUNTA = re.compile(r"pregunta|ítem|item", re.IGNORECASE)


def sin_tiempo():
    return time.time() - INICIO > TIEMPO_MAX


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers


def canvas(ruta):
    """Trae todas las páginas de una consulta a Canvas y devuelve una lista."""
    resultados, url = [], CANVAS_URL + ruta
    while url:
        cuerpo, cabeceras = http(url, {"Authorization": f"Bearer {CANVAS_TOKEN}"})
        datos = json.loads(cuerpo)
        resultados += datos if isinstance(datos, list) else [datos]
        url = None
        for parte in (cabeceras.get("Link") or "").split(","):
            if 'rel="next"' in parte:
                url = parte[parte.index("<") + 1 : parte.index(">")]
    return resultados


def limpio(texto_html):
    texto = re.sub(r"<[^>]+>", " ", texto_html or "")
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def local(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZONA)


def leer_archivo(nombre, datos):
    """Devuelve el texto del archivo, o None si es una pauta o un enunciado de control."""
    if nombre.lower().endswith(".docx"):
        doc = Document(io.BytesIO(datos))
        partes = [p.text for p in doc.paragraphs]
        partes += [" | ".join(c.text for c in fila.cells) for tabla in doc.tables for fila in tabla.rows]
    else:
        lector = PdfReader(io.BytesIO(datos))
        partes = []
        for i, pagina in enumerate(lector.pages):
            if i >= PAGINAS_MAX:
                break
            partes.append(pagina.extract_text() or "")
    texto = "\n".join(partes)
    inicio = texto[:1000]  # el título y lo de arriba suelen decir qué tipo de documento es
    if DESCARTAR_INICIO.search(inicio) or (PALABRAS_EXAMEN.search(inicio) and PALABRAS_PREGUNTA.search(inicio)):
        return None
    return texto


def token_gmail():
    datos = urllib.parse.urlencode({
        "client_id": GMAIL_ID,
        "client_secret": GMAIL_SECRET,
        "refresh_token": GMAIL_REFRESH,
        "grant_type": "refresh_token",
    }).encode()
    cuerpo, _ = http("https://oauth2.googleapis.com/token",
                     {"Content-Type": "application/x-www-form-urlencoded"}, datos)
    return json.loads(cuerpo)["access_token"]


def cuerpo_correo(parte):
    mime = parte.get("mimeType", "")
    datos = parte.get("body", {}).get("data")
    if mime in ("text/plain", "text/html") and datos:
        relleno = "=" * (-len(datos) % 4)
        texto = base64.urlsafe_b64decode(datos + relleno).decode("utf-8", "ignore")
        return texto if mime == "text/plain" else limpio(texto)
    return "\n".join(cuerpo_correo(p) for p in parte.get("parts", []))


def correos(token):
    cabecera = {"Authorization": f"Bearer {token}"}
    busqueda = urllib.parse.quote("newer_than:30d -category:promotions -category:social")
    cuerpo, _ = http(f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={busqueda}&maxResults=40", cabecera)
    for m in json.loads(cuerpo).get("messages", []):
        cuerpo, _ = http(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}?format=full", cabecera)
        msg = json.loads(cuerpo)
        asunto = next((h["value"] for h in msg["payload"].get("headers", [])
                       if h["name"].lower() == "subject"), "(sin asunto)")
        yield asunto, f"Asunto: {asunto}\n{cuerpo_correo(msg['payload'])}"


def llamar_gemini(instruccion):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    datos = json.dumps({
        "contents": [{"parts": [{"text": instruccion}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode()
    for intento in range(3):
        try:
            cuerpo, _ = http(url, {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}, datos)
            return json.loads(cuerpo)["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as err:
            if err.code != 429 or intento == 2:
                raise
            espera = 20 * (intento + 1)
            print(f"Límite de Gemini alcanzado, espero {espera} segundos")
            time.sleep(espera)


def fechas_con_gemini(texto, origen):
    instruccion = (
        f"Hoy es {HOY.isoformat()}. Este texto viene de un curso universitario en Chile ({origen}). "
        "Saca TODAS las fechas importantes: pruebas, controles, certámenes, entregas, plazos e informes. "
        "Usa solo fechas que aparezcan explícitamente. Responde SOLO con un arreglo JSON, sin texto extra, "
        'con objetos así: {"fecha": "AAAA-MM-DD", "hora": "HH:MM" o null, "titulo": "texto corto", '
        '"tipo": "prueba" | "entrega" | "aviso"}. Si no hay fechas, responde [].\n\n'
        + texto[:30000]
    )
    respuesta = llamar_gemini(instruccion)
    inicio, fin = respuesta.find("["), respuesta.rfind("]")
    return json.loads(respuesta[inicio : fin + 1]) if inicio != -1 else []


def normalizar(e, origen):
    try:
        fecha = dt.date.fromisoformat(e["fecha"]).isoformat()
    except (KeyError, ValueError, TypeError):
        return None
    hora = None
    try:
        h, m = str(e.get("hora") or "").split(":")[:2]
        hora = f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    return {"fecha": fecha, "hora": hora, "titulo": str(e.get("titulo") or "Sin título")[:120],
            "tipo": e.get("tipo", "aviso"), "origen": origen}


def limpiar_ics(texto):
    texto = (texto or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return texto.replace("\r", "").replace("\n", "\\n")


def generar_ics(eventos):
    ahora = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lineas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//fechas-canvas//ES", "X-WR-CALNAME:Fechas Canvas"]
    for e in eventos:
        d = dt.date.fromisoformat(e["fecha"])
        uid = hashlib.sha1(f"{e['fecha']}{e['hora']}{e['titulo']}".encode("utf-8")).hexdigest()
        lineas += ["BEGIN:VEVENT", f"UID:{uid}@fechas-canvas", f"DTSTAMP:{ahora}"]
        if e["hora"]:
            inicio = dt.datetime.combine(d, dt.time(*map(int, e["hora"].split(":"))))
            lineas += [f"DTSTART:{inicio:%Y%m%dT%H%M%S}",
                       f"DTEND:{inicio + dt.timedelta(hours=1):%Y%m%dT%H%M%S}"]
        else:
            lineas += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                       f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}"]
        lineas += [
            f"SUMMARY:{limpiar_ics(e['titulo'])}",
            f"DESCRIPTION:{limpiar_ics(e['tipo'] + ' - ' + e['origen'])}",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Mañana: {limpiar_ics(e['titulo'])}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lineas.append("END:VCALENDAR")
    return "\r\n".join(lineas) + "\r\n"


def guardar_cache(datos):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


def main():
    viejo = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    nuevo, eventos, textos = dict(viejo), [], []
    fallidas, pendientes, seguidos_fallidos = 0, 0, 0
    omitidos_nombre, omitidos_contenido = 0, 0

    # 1) Canvas: fechas exactas de tareas, y textos de anuncios, archivos útiles y páginas
    for curso in canvas("/api/v1/courses?enrollment_state=active&per_page=50"):
        if sin_tiempo():
            break
        if not curso.get("id"):
            continue
        cid, nombre = curso["id"], curso.get("name", "Curso")
        try:
            for t in canvas(f"/api/v1/courses/{cid}/assignments?per_page=100"):
                if t.get("due_at"):
                    f = local(t["due_at"])
                    eventos.append({"fecha": f.date().isoformat(), "hora": f.strftime("%H:%M"),
                                    "titulo": t.get("name", "Entrega"), "tipo": "entrega",
                                    "origen": nombre})
        except Exception as err:
            print(f"Tareas de {nombre}: {err}")
        try:
            for a in canvas(f"/api/v1/announcements?context_codes[]=course_{cid}&per_page=20"):
                textos.append((f"{nombre} - anuncio", f"{a.get('title', '')}\n{limpio(a.get('message'))}"))
        except Exception as err:
            print(f"Anuncios de {nombre}: {err}")
        try:
            for modulo in canvas(f"/api/v1/courses/{cid}/modules?include[]=items&per_page=100"):
                for item in modulo.get("items", []):
                    if sin_tiempo():
                        break
                    if item.get("type") == "File":
                        nombre_archivo = item.get("title", "")
                        es_util = (nombre_archivo.lower().endswith((".pdf", ".docx"))
                                   and PALABRAS_FECHAS.search(nombre_archivo)
                                   and not DESCARTAR_NOMBRE.search(nombre_archivo))
                        if not es_util:
                            omitidos_nombre += 1  # ni siquiera se descarga
                            continue
                        archivo = canvas(f"/api/v1/files/{item['content_id']}")[0]
                        datos, _ = http(archivo["url"])
                        texto = leer_archivo(nombre_archivo, datos)
                        if texto is None:
                            omitidos_contenido += 1  # era pauta o enunciado de control
                            continue
                        textos.append((f"{nombre} - {nombre_archivo}", texto))
                    elif item.get("type") == "Page":
                        pagina = canvas(f"/api/v1/courses/{cid}/pages/{item['page_url']}")[0]
                        textos.append((f"{nombre} - {pagina.get('title', 'página')}", limpio(pagina.get("body"))))
        except Exception as err:
            print(f"Módulos de {nombre}: {err}")

    # 2) Gmail: correos de los últimos 30 días
    if GMAIL_REFRESH and not sin_tiempo():
        try:
            for asunto, texto in correos(token_gmail()):
                textos.append((f"Correo: {asunto}", texto))
        except Exception as err:
            print(f"Gmail: {err}")

    # 3) Gemini saca las fechas de los textos nuevos; lo ya leído sale de la cache
    if not GEMINI_KEY:
        print("Sin GEMINI_API_KEY: solo se usaron las fechas de las tareas.")
    for origen, texto in textos:
        if not texto.strip():
            continue
        clave = hashlib.sha1((origen + texto).encode("utf-8")).hexdigest()
        if clave not in nuevo and GEMINI_KEY:
            if sin_tiempo() or seguidos_fallidos >= 3:
                pendientes += 1
                continue
            time.sleep(PAUSA_GEMINI)
            try:
                nuevo[clave] = fechas_con_gemini(texto, origen)
                seguidos_fallidos = 0
                guardar_cache(nuevo)  # se guarda con cada éxito, por si la corrida se corta
            except Exception as err:
                fallidas += 1
                seguidos_fallidos += 1
                print(f"No pude leer {origen}: {err}")
                continue
        if clave in nuevo:
            eventos += [normalizar(e, origen) for e in nuevo[clave]]

    guardar_cache(nuevo)

    # 4) Quitar repetidas y fechas pasadas, y guardar el calendario
    vistos, finales = set(), []
    for e in sorted((e for e in eventos if e), key=lambda x: (x["fecha"], x["hora"] or "")):
        clave = (e["fecha"], e["hora"], e["titulo"].lower())
        if e["fecha"] >= HOY.isoformat() and clave not in vistos:
            vistos.add(clave)
            finales.append(e)

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(generar_ics(finales), encoding="utf-8")
    print(f"Fuentes de texto: {len(textos)}. Omitidos por nombre: {omitidos_nombre}. "
          f"Omitidos por ser pauta o control: {omitidos_contenido}. Fallidas: {fallidas}. "
          f"Pendientes para la próxima corrida: {pendientes}. Fechas en el calendario: {len(finales)}.")


if __name__ == "__main__":
    main()
