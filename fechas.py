"""Lee las fechas próximas de Canvas y genera docs/fechas.ics (un calendario que Google Calendar puede leer)."""
import datetime as dt
import json
import os
import pathlib
import urllib.request

CANVAS_URL = os.environ["CANVAS_URL"].rstrip("/")
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
SALIDA = pathlib.Path("docs/fechas.ics")


def pedir(ruta):
    req = urllib.request.Request(CANVAS_URL + ruta, headers={"Authorization": f"Bearer {CANVAS_TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def fecha(texto):
    return dt.datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def formato_ics(d):
    return d.strftime("%Y%m%dT%H%M%SZ")


def limpiar(texto):
    texto = (texto or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return texto.replace("\r", "").replace("\n", "\\n")


def main():
    ahora = dt.datetime.now(dt.timezone.utc)
    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fechas-canvas//ES",
        "X-WR-CALNAME:Fechas Canvas",
    ]
    total = 0

    for e in pedir("/api/v1/users/self/upcoming_events"):
        tarea = e.get("assignment") or {}
        inicio_txt = e.get("start_at") or tarea.get("due_at")
        if not inicio_txt:
            continue

        inicio = fecha(inicio_txt)
        if inicio < ahora:
            continue

        fin = fecha(e["end_at"]) if e.get("end_at") else inicio + dt.timedelta(hours=1)
        if fin <= inicio:
            fin = inicio + dt.timedelta(hours=1)

        titulo = e.get("title") or tarea.get("name") or "Sin título"
        tipo = "Entrega" if tarea else "Evento"
        ident = tarea.get("id") or e.get("id") or titulo

        lineas += [
            "BEGIN:VEVENT",
            f"UID:{ident}-{formato_ics(inicio)}@fechas-canvas",
            f"DTSTAMP:{formato_ics(ahora)}",
            f"DTSTART:{formato_ics(inicio)}",
            f"DTEND:{formato_ics(fin)}",
            f"SUMMARY:{limpiar(titulo)}",
            f"DESCRIPTION:{limpiar(tipo + ' - ' + (e.get('context_name') or ''))}",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Mañana: {limpiar(titulo)}",
            "END:VALARM",
            "END:VEVENT",
        ]
        total += 1

    lineas.append("END:VCALENDAR")
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text("\r\n".join(lineas) + "\r\n", encoding="utf-8")
    print(f"Listo: {total} fechas guardadas en {SALIDA}.")


if __name__ == "__main__":
    main()
