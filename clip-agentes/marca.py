"""Tu logo personal: se guarda una vez y el agente Marca lo pone en cada video."""
import base64
import json
import re
import subprocess
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
ASSETS = BASE / "assets"
LOGO = ASSETS / "logo.png"
CONFIG = BASE / "marca.json"
DEFAULTS = {"pos": "top-left", "size": 16, "opacity": 100, "handle": ""}
POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
MAX_BYTES = 8 * 1024 * 1024
MARGIN = 48
lock = threading.Lock()


class BrandError(Exception):
    pass


def load():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: data[k] for k in DEFAULTS if k in data}}


def has_logo():
    return LOGO.is_file()


def public():
    cfg = load()
    return {**cfg, "has_logo": has_logo(), "version": int(LOGO.stat().st_mtime) if has_logo() else 0}


def save_settings(body):
    cfg = load()
    if body.get("pos") in POSITIONS:
        cfg["pos"] = body["pos"]
    for key, lo, hi in (("size", 6, 24), ("opacity", 20, 100)):
        if key in body:
            try:
                cfg[key] = max(lo, min(hi, int(body[key])))
            except (TypeError, ValueError):
                raise BrandError(f"'{key}' debe ser un numero.")
    if "handle" in body:
        handle = re.sub(r"[^\w.]", "", str(body["handle"]).lstrip("@"))[:30]
        cfg["handle"] = f"@{handle}" if handle else ""
    with lock:
        CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def save_logo(ffmpeg, data_url):
    """Recibe la imagen en base64, la valida con ffmpeg y la guarda como PNG."""
    m = re.match(r"data:image/(png|jpe?g|webp|gif|bmp);base64,(.+)$", str(data_url or ""), re.S)
    if not m:
        raise BrandError("Sube una imagen PNG, JPG o WEBP.")
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        raise BrandError("La imagen esta danada.")
    if not raw or len(raw) > MAX_BYTES:
        raise BrandError("La imagen debe pesar menos de 8 MB.")
    ASSETS.mkdir(exist_ok=True)
    tmp = ASSETS / f"subida.{m.group(1)}"
    tmp.write_bytes(raw)
    out = ASSETS / "nuevo.png"
    try:
        proc = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(tmp), "-frames:v", "1",
                               "-vf", "scale='min(1024,iw)':-2", str(out)], capture_output=True, text=True)
        if proc.returncode != 0 or not out.is_file():
            raise BrandError("No pude leer esa imagen. Prueba con un PNG.")
        out.replace(LOGO)
    finally:
        tmp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def clear_logo():
    LOGO.unlink(missing_ok=True)


def overlay_graph(width=1080, height=1920):
    """Filtro de ffmpeg que pone el logo sobre el video ([0:v] video, [1:v] logo) y deja la salida en [v]."""
    cfg = load()
    w = int(width * cfg["size"] / 100)
    x = str(MARGIN) if cfg["pos"].endswith("left") else f"W-w-{MARGIN}"
    y = "60" if cfg["pos"].startswith("top") else f"H-h-{MARGIN + 300}"
    alpha = cfg["opacity"] / 100
    return (f"[1:v]scale={w}:-2,format=rgba,colorchannelmixer=aa={alpha:.2f}[lg];"
            f"[0:v][lg]overlay=x={x}:y={y}:format=auto[v]")
