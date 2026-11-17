"""Radar viral: busca videos y directos con muchas vistas, likes o espectadores, te avisa por correo con el enlace
y, si no hay ninguna sesion iniciada, baja el video a tu PC.

Toda la configuracion (incluida la clave del correo) se guarda solo en radar.json, en tu PC.
"""
import html
import json
import re
import smtplib
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

import publicar
import secreto

BASE = Path(__file__).resolve().parent
FILE = BASE / "radar.json"
DOWNLOADS = BASE / "videos" / "virales"

SEARCH = "https://www.youtube.com/results?search_query={q}&sp={sp}"
SP_TODAY, SP_WEEK, SP_LIVE = "CAMSAggC", "CAMSAggD", "EgJAAQ%253D%253D"   # por vistas hoy / esta semana; directos
FOUND_MAX = 200

DEFAULTS = {
    "enabled": False,
    "threshold": 100000,
    "interval": 20,
    "queries": ["viral", "trending"],
    "live_queries": ["live", "en vivo"],
    "watch": [],
    "auto_download": True,
    "max_minutes": 30,
    "email": {"to": "", "host": "smtp.gmail.com", "port": 587, "user": "", "password": ""},
    "found": [],
}

_lock = threading.RLock()
_scan_lock = threading.Lock()
_status = {"running": False, "last": 0.0, "error": "", "new": 0}
_tools = {"ytdlp": None, "ffmpeg": None}
_pool = ThreadPoolExecutor(2)


class RadarError(Exception):
    pass


def configure(ytdlp, ffmpeg):
    _tools["ytdlp"], _tools["ffmpeg"] = ytdlp, ffmpeg


# ---------- configuracion ----------
def _load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg.update({k: v for k, v in data.items() if k in DEFAULTS and k != "email"})
    cfg["email"].update(data.get("email") or {})
    try:
        cfg["email"]["password"] = secreto.unprotect(str(cfg["email"]["password"]))
    except (OSError, ValueError):
        cfg["email"]["password"] = ""   # cifrada por otro usuario o equipo: hay que escribirla de nuevo
    return cfg


def _save(cfg):
    out = json.loads(json.dumps(cfg))
    out["email"]["password"] = secreto.protect(str(out["email"].get("password", ""))) if out["email"].get("password") else ""
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def public():
    with _lock:
        cfg = _load()
    email = dict(cfg["email"])
    email["has_password"] = bool(email.pop("password", ""))
    keys = ("enabled", "threshold", "interval", "queries", "live_queries", "watch", "auto_download", "max_minutes")
    status = dict(_status)
    status["next_in"] = (max(0, int(status["last"] + cfg["interval"] * 60 - time.time()))
                         if cfg["enabled"] and status["last"] and not status["running"] else None)
    return {**{k: cfg[k] for k in keys}, "email": email, "status": status, "found": cfg["found"][:40]}


def _lines(value, limit, maxlen):
    items = value if isinstance(value, list) else str(value or "").splitlines()
    out = []
    for item in items:
        text = re.sub(r"\s+", " ", str(item)).strip()[:maxlen]
        if text and text not in out:
            out.append(text)
    return out[:limit]


def save_settings(body):
    with _lock:
        cfg = _load()
        try:
            threshold = int(body.get("threshold", cfg["threshold"]))
            interval = int(body.get("interval", cfg["interval"]))
            max_minutes = int(body.get("max_minutes", cfg["max_minutes"]))
        except (TypeError, ValueError):
            raise RadarError("El minimo de vistas, el intervalo y los minutos deben ser numeros.")
        if threshold < 1:
            raise RadarError("El minimo debe ser al menos 1.")
        if not 5 <= interval <= 720:
            raise RadarError("Revisar cada: entre 5 y 720 minutos.")
        if not 1 <= max_minutes <= 600:
            raise RadarError("Duracion maxima a bajar: entre 1 y 600 minutos.")
        watch = _lines(body.get("watch", cfg["watch"]), 10, 300)
        bad = [w for w in watch if not re.fullmatch(r"https?://\S+", w)]
        if bad:
            raise RadarError(f"Eso no es un enlace: {bad[0][:60]}")
        e_in = body.get("email") or {}
        email = dict(cfg["email"])
        to = str(e_in.get("to", email["to"])).strip()
        if to and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise RadarError("El correo de destino no es valido.")
        host = str(e_in.get("host", email["host"])).strip()
        if host and not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise RadarError("El servidor SMTP no es valido.")
        try:
            port = int(e_in.get("port", email["port"]))
        except (TypeError, ValueError):
            raise RadarError("El puerto debe ser un numero.")
        if not 1 <= port <= 65535:
            raise RadarError("El puerto debe estar entre 1 y 65535.")
        email.update(to=to, host=host, port=port, user=str(e_in.get("user", email["user"])).strip()[:200])
        if str(e_in.get("password", "")).strip():
            email["password"] = str(e_in["password"]).strip()[:200]
        cfg.update(enabled=bool(body.get("enabled", cfg["enabled"])), threshold=threshold, interval=interval,
                   max_minutes=max_minutes, auto_download=bool(body.get("auto_download", cfg["auto_download"])),
                   queries=_lines(body.get("queries", cfg["queries"]), 10, 80),
                   live_queries=_lines(body.get("live_queries", cfg["live_queries"]), 10, 80),
                   watch=watch, email=email)
        _save(cfg)


def clear_found():
    with _lock:
        cfg = _load()
        cfg["found"] = []
        _save(cfg)


# ---------- correo ----------
def send_mail(cfg, subject, text, body_html):
    e = cfg["email"]
    if not (e["to"] and e["host"] and e["user"] and e["password"]):
        raise RadarError("Falta configurar el correo: destino, servidor, usuario y clave.")
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"\s+", " ", subject)[:200]
    msg["From"] = e["user"] if "@" in e["user"] else e["to"]
    msg["To"] = e["to"]
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    ctx = ssl.create_default_context()
    try:
        if e["port"] == 465:
            smtp = smtplib.SMTP_SSL(e["host"], 465, timeout=30, context=ctx)
        else:
            smtp = smtplib.SMTP(e["host"], e["port"], timeout=30)
            smtp.starttls(context=ctx)
        with smtp:
            smtp.login(e["user"], e["password"])
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RadarError("El servidor rechazo el usuario o la clave. En Gmail hay que usar una "
                         "'contrasena de aplicacion', no la clave normal.")
    except (OSError, smtplib.SMTPException) as exc:
        raise RadarError(f"No pude enviar el correo: {str(exc)[:160]}")


def send_test():
    with _lock:
        cfg = _load()
    send_mail(cfg, "[Radar viral] Correo de prueba",
              "Si lees esto, el Radar viral ya puede avisarte por correo.",
              "<p>Si lees esto, el <b>Radar viral</b> ya puede avisarte por correo.</p>")


def _n(v):
    return f"{v / 1e6:.1f} M" if v >= 1e6 else (f"{v / 1e3:.0f} mil" if v >= 1e3 else str(v))


def _notify(cfg, rec):
    what = {"live": "personas viendo en vivo", "likes": "likes", "views": "vistas"}[rec["metric"]]
    kind = "DIRECTO" if rec["kind"] == "live" else "VIDEO"
    lines = [f"{kind} con {_n(rec['value'])} {what}", "", rec["title"]]
    if rec["channel"]:
        lines.append(f"Canal: {rec['channel']}")
    lines += ["", f"Enlace: {rec['url']}"]
    if rec["download"] == "bajando":
        lines += ["", "No hay ninguna sesion iniciada, asi que se esta guardando en tu PC (carpeta videos/virales)."]
    text = "\n".join(lines)
    esc = html.escape
    body = (f"<h3>{kind} con {_n(rec['value'])} {what}</h3><p><b>{esc(rec['title'])}</b>"
            + (f"<br>Canal: {esc(rec['channel'])}" if rec["channel"] else "")
            + f"</p><p><a href=\"{esc(rec['url'])}\">{esc(rec['url'])}</a></p>"
            + ("<p>No hay ninguna sesion iniciada: se esta guardando en tu PC (carpeta videos/virales).</p>"
               if rec["download"] == "bajando" else ""))
    send_mail(cfg, f"[Radar viral] {kind}: {rec['title']} ({_n(rec['value'])})", text, body)


# ---------- busqueda ----------
def _yt(args, timeout=150):
    cmd = _tools["ytdlp"]
    if not cmd:
        raise RadarError("yt-dlp no esta instalado. Corre: pip install yt-dlp")
    proc = subprocess.run([*cmd, "--no-warnings", "--socket-timeout", "20", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if not proc.stdout.strip():
        raise RadarError((proc.stderr.strip().splitlines() or ["yt-dlp no devolvio nada"])[-1][:200])
    return json.loads(proc.stdout)


def _search(query, sp, count):
    data = _yt(["-J", "--flat-playlist", "--playlist-end", str(count), "--",
                SEARCH.format(q=quote_plus(query), sp=sp)])
    return data.get("entries") or []


def _leaves(info, depth=0):
    for entry in (info.get("entries") or []) if depth < 3 else []:
        if entry:
            yield from _leaves(entry, depth + 1)
    if info.get("id") and not info.get("entries"):
        yield info


def _record(info, kind, metric, value):
    vid = info.get("id") or ""
    url = info.get("webpage_url") or info.get("url") or ""
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    ie = info.get("extractor_key") or info.get("ie_key") or "Youtube"
    return {"id": f"{ie}:{vid}", "url": url, "title": str(info.get("title") or url)[:200],
            "channel": str(info.get("channel") or info.get("uploader") or "")[:120], "kind": kind,
            "metric": metric, "value": int(value), "duration": info.get("duration") or 0, "ts": int(time.time()),
            "emailed": "", "download": ""}


def _from_info(info, threshold):
    """Devuelve el registro si este video o directo pasa el minimo, o None."""
    if info.get("is_live") or info.get("live_status") == "is_live":
        viewers = info.get("concurrent_view_count") or info.get("view_count") or 0
        return _record(info, "live", "live", viewers) if viewers >= threshold else None
    views, likes = info.get("view_count") or 0, info.get("like_count") or 0
    if max(views, likes) < threshold:
        return None
    return _record(info, "video", "views" if views >= likes else "likes", max(views, likes))


def _scan(cfg):
    th, hits, errors = cfg["threshold"], {}, []

    def keep(rec):
        if rec:
            hits.setdefault(rec["id"], rec)

    for query in cfg["queries"]:
        for sp in (SP_TODAY, SP_WEEK):
            try:
                for e in _search(query, sp, 15):
                    if (e.get("view_count") or 0) >= th and e.get("live_status") != "is_live":
                        keep(_from_info(e, th))
            except Exception as exc:
                errors.append(str(exc)[:120])

    live_urls = []
    for query in cfg["live_queries"]:
        try:
            live_urls += [e.get("url") for e in _search(query, SP_LIVE, 12) if e.get("url")]
        except Exception as exc:
            errors.append(str(exc)[:120])

    def live_info(url):
        try:
            return _from_info(_yt(["-J", "--no-playlist", "--", url]), th)
        except Exception as exc:
            errors.append(str(exc)[:120])

    def watch_info(url):
        try:
            for leaf in _leaves(_yt(["-J", "--playlist-end", "5", "--", url], timeout=300)):
                keep(_from_info(leaf, th))
        except Exception as exc:
            errors.append(str(exc)[:120])

    with ThreadPoolExecutor(4) as pool:
        for rec in pool.map(live_info, list(dict.fromkeys(live_urls))[:20]):
            keep(rec)
        list(pool.map(watch_info, cfg["watch"]))
    return list(hits.values()), errors


def _update(rec_id, **fields):
    with _lock:
        cfg = _load()
        for r in cfg["found"]:
            if r["id"] == rec_id:
                r.update(fields)
        _save(cfg)


def _download(rec, max_minutes):
    if rec["duration"] and rec["duration"] > max_minutes * 60:
        return _update(rec["id"], download=f"omitido: dura {int(rec['duration'] // 60)} min")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    args = [*_tools["ytdlp"], "-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4",
            "--no-playlist", "--windows-filenames", "--no-warnings", "--no-simulate",
            "--print", "after_move:filepath", "-o", str(DOWNLOADS / "%(title).80B [%(id)s].%(ext)s")]
    if _tools["ffmpeg"]:
        args += ["--ffmpeg-location", str(Path(_tools["ffmpeg"]).parent)]
    try:
        proc = subprocess.run([*args, "--", rec["url"]], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=3600)
        path = (proc.stdout.strip().splitlines() or [""])[-1]
        if proc.returncode or not Path(path).is_file():
            raise RadarError((proc.stderr.strip().splitlines() or ["no se pudo bajar"])[-1][:160])
        _update(rec["id"], download="listo: " + Path(path).name)
    except Exception as exc:
        _update(rec["id"], download="error: " + str(exc)[:160])


def _process(cfg, recs):
    with _lock:
        stored = _load()
        known = {r["id"] for r in stored["found"]}
        new = [r for r in recs if r["id"] not in known]
        if not new:
            return 0
        for rec in new:
            if (rec["kind"] == "video" and cfg["auto_download"] and _tools["ytdlp"]
                    and not publicar.linked_networks()):
                rec["download"] = "bajando"
        stored["found"] = (new + stored["found"])[:FOUND_MAX]
        _save(stored)
    for rec in new:
        e = cfg["email"]
        if e["to"] and e["host"] and e["user"] and e["password"]:
            try:
                _notify(cfg, rec)
                _update(rec["id"], emailed="enviado")
            except RadarError as exc:
                _update(rec["id"], emailed=f"error: {exc}"[:200])
        if rec["download"] == "bajando":
            _pool.submit(_download, rec, cfg["max_minutes"])
    return len(new)


def _run_scan():
    try:
        with _lock:
            cfg = _load()
        recs, errors = _scan(cfg)
        _status["new"] = _process(cfg, recs)
        _status["error"] = errors[0] if errors and not recs else ""
    except Exception as exc:
        _status["error"] = str(exc)[:200]
    finally:
        _status["running"] = False
        _status["last"] = time.time()
        _scan_lock.release()


def scan_now():
    """Empieza una busqueda en segundo plano. Devuelve False si ya hay una en marcha."""
    if not _scan_lock.acquire(blocking=False):
        return False
    _status.update(running=True, last=time.time(), error="")
    threading.Thread(target=_run_scan, daemon=True).start()
    return True


def _loop():
    while True:
        time.sleep(15)
        with _lock:
            cfg = _load()
        if cfg["enabled"] and not _status["running"] and time.time() - _status["last"] >= cfg["interval"] * 60:
            scan_now()


def start():
    threading.Thread(target=_loop, daemon=True).start()
