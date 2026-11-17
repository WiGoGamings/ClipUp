"""Clip Agentes - backend. Sirve el panel y ejecuta los 9 agentes.

Uso:  python server.py   ->  http://127.0.0.1:8765
Necesita: yt-dlp y ffmpeg (descarga/edicion), faster-whisper (transcripcion),
deep-translator (traduccion) y edge-tts (voz).
"""
import atexit
import contextlib
import importlib.util
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import cerebro
import envivo
import ia
import marca
import publicar
import radar
import reaccion
import rutas

BASE = rutas.DATA
VIDEOS = BASE / "videos"
CLIPS = BASE / "clips"
WORK = BASE / "trabajo"
PUBLISHED = BASE / "publicados"
JOBS_FILE = BASE / "jobs.json"
PANEL = rutas.RES / "panel.html"
PORT = publicar.PORT

AGENTS = [
    ("receiver", "Receptor", "Baja tu video con yt-dlp", ["#FF2E88", "#ffb36b"]),
    ("scout", "Cazador viral", "IA + reacciones del publico", ["#ff5c5c", "#ffe36b"]),
    ("cleaner", "Limpiador", "Quita silencios y empareja volumen", ["#00E5FF", "#8a2cff"]),
    ("editor", "Editor", "Formato vertical 9:16 y titulo", ["#ffe36b", "#FF2E88"]),
    ("enhancer", "Mejorador", "Color, nitidez y 1080x1920", ["#3ddc97", "#00E5FF"]),
    ("brand", "Marca", "Pone tu logo en cada video", ["#b98bff", "#00E5FF"]),
    ("translator", "Traductor", "Transcribe y traduce a ES y EN", ["#b98bff", "#FF2E88"]),
    ("voice", "Voz", "Doblaje, subtitulos y titulo", ["#ffb36b", "#8a2cff"]),
    ("marketing", "Marketing", "IA: titulo, descripcion, hashtags", ["#00E5FF", "#3ddc97"]),
    ("checkin", "Check-in", "Revisa todo antes de subir", ["#ffe36b", "#3ddc97"]),
    ("publisher", "Publicador", "Sube a tus redes vinculadas", ["#FF2E88", "#b98bff"]),
]
AGENT_KEYS = [a[0] for a in AGENTS]
AGENT_NAMES = {a[0]: a[1] for a in AGENTS}
LANGS = (("es", "Espanol"), ("en", "Ingles"))

SILENCE_DB = "-40dB"
SILENCE_MIN = 0.5
SILENCE_PAD = 0.15
MAX_SECONDS = 60
DUCK_VOLUME = 0.10
SPLIT_EVERY = 180
SPLIT_MAX = 60

lock = threading.RLock()
jobs = []
LIVES = {}
tasks = queue.Queue()


class StepError(Exception):
    pass


class Cancelled(Exception):
    pass


PROCS = {}


# ---------- herramientas ----------
@lru_cache(maxsize=None)
def find_tool(name):
    bundled = rutas.APP_DIR / "ffmpeg" / f"{name}.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    for hit in local.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(hit)
    return None


@lru_cache(maxsize=None)
def ytdlp_cmd():
    if rutas.FROZEN:
        return [sys.executable, "--ytdlp"]   # el propio programa hace de yt-dlp (ver app.py)
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    return [exe] if exe else None


def tools_status():
    have = lambda mod: importlib.util.find_spec(mod) is not None
    return {"ffmpeg": bool(find_tool("ffmpeg")), "ytdlp": bool(ytdlp_cmd()),
            "whisper": have("faster_whisper"), "translate": have("deep_translator"), "voice": have("edge_tts")}


def run(cmd, cwd=None, job=None):
    """Ejecuta un comando. Si pasas el trabajo, se puede abortar al cancelarlo."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    if job:
        with lock:
            PROCS.setdefault(job["id"], set()).add(proc)
        if job.get("cancelled"):
            proc.kill()
    try:
        out, err = proc.communicate()
    finally:
        if job:
            with lock:
                PROCS.get(job["id"], set()).discard(proc)
    if job and job.get("cancelled"):
        raise Cancelled()
    if proc.returncode != 0:
        tail = "\n".join((err or out).strip().splitlines()[-8:])
        raise StepError(tail or f"El comando fallo (codigo {proc.returncode})")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def ffmpeg(args, cwd=None, job=None):
    exe = find_tool("ffmpeg")
    if not exe:
        raise StepError("ffmpeg no esta instalado o no esta en el PATH. Instalalo desde ffmpeg.org.")
    return run([exe, "-hide_banner", "-y", *args], cwd=cwd, job=job)


HW_ARGS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                   "-maxrate", "14M", "-bufsize", "28M"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "vbr_peak", "-b:v", "10M", "-maxrate", "14M"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "22", "-maxrate", "14M"],
}


@lru_cache(maxsize=None)
def hw_encoder():
    """Detecta si la tarjeta de video puede codificar H.264 (mucho mas rapido que el procesador)."""
    exe = find_tool("ffmpeg")
    if not exe or os.environ.get("CLIP_SW") == "1":
        return None
    for name, args in HW_ARGS.items():
        test = subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=0.3",
                               *args, "-pix_fmt", "yuv420p", "-f", "null", "-"], capture_output=True)
        if test.returncode == 0:
            return name
    return None


def venc(crf=20):
    hw = hw_encoder()
    codec = HW_ARGS[hw] if hw else ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                                    "-maxrate", "14M", "-bufsize", "28M"]
    return [*codec, "-pix_fmt", "yuv420p"]


def probe_full(path):
    exe = find_tool("ffprobe")
    if not exe:
        raise StepError("ffprobe no esta instalado (viene con ffmpeg).")
    out = run([exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
               "-of", "json", str(path)]).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"duration": float(data["format"]["duration"]),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "width": video.get("width"), "height": video.get("height")}


def probe(path):
    info = probe_full(path)
    return info["duration"], info["has_audio"]


# ---------- trabajos ----------
def save():
    with lock:
        JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")


def load():
    if not JOBS_FILE.exists():
        return
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for job in data:
        if job["status"] in ("processing", "awaiting"):
            job["status"] = "failed"
            job["error"] = "El servidor se cerro mientras procesaba. Procesa el video de nuevo."
            for key in AGENT_KEYS:
                if job["agents"].get(key) == "working":
                    job["agents"][key] = "failed"
            if job["checkin"]["state"] == "checking":
                job["checkin"]["state"] = "idle"
        jobs.append(job)


def get_job(job_id):
    with lock:
        return next((j for j in jobs if j["id"] == job_id), None)


def log(job, msg):
    with lock:
        job["log"] = (job["log"] + [time.strftime("%H:%M:%S ") + msg])[-60:]
    save()


def set_agent(job, key, state):
    with lock:
        job["agents"][key] = state
    save()


def set_status(job, status):
    """Cambia el estado, salvo que el trabajo ya este cancelado."""
    with lock:
        if not job.get("cancelled"):
            job["status"] = status


@contextlib.contextmanager
def step(job, key, label):
    if job.get("cancelled"):
        raise Cancelled()
    set_agent(job, key, "working")
    log(job, label)
    started = time.time()
    try:
        yield
    except Cancelled:
        raise
    except Exception as exc:
        set_agent(job, key, "failed")
        set_status(job, "failed")
        with lock:
            job["error"] = f"{AGENT_NAMES[key]}: {exc}"
        log(job, f"ERROR - {exc}")
        raise
    if job.get("cancelled"):
        raise Cancelled()
    set_agent(job, key, "done")
    log(job, f"{AGENT_NAMES[key]} listo en {time.time() - started:.1f}s")


def cancel_job(job):
    with lock:
        if job.get("cancelled"):
            return
        job["cancelled"] = True
        job["status"] = "cancelled"
        job["error"] = ""
        for key, state in job["agents"].items():
            if state == "working":
                job["agents"][key] = "pending"
        procs = list(PROCS.get(job["id"], ()))
    for proc in procs:
        with contextlib.suppress(Exception):
            proc.kill()
    log(job, "Cancelaste este video.")


def new_job(url, owner, auto, live=None):
    job_id = time.strftime("%Y%m%d-%H%M%S")
    while get_job(job_id):
        job_id += "x"
    job = {
        "id": job_id, "url": url, "title": "Procesando...", "status": "processing",
        "duration": 0, "error": "", "proposal": "", "owner": bool(owner), "auto": bool(auto),
        "live": live, "cancelled": False, "viral": None,
        "files": {}, "versions": {}, "tr": None, "publish": [],
        "checkin": {"state": "idle", "checks": []},
        "agents": {k: "pending" for k in AGENT_KEYS}, "log": [],
    }
    with lock:
        jobs.insert(0, job)
    save()
    return job


# ---------- estaciones 1-4: recibir, limpiar, editar, mejorar ----------
def receive(job):
    url = job["url"]
    local = Path(url.strip('"'))
    if local.is_file():
        job["files"]["raw"] = str(local)
        job["title"] = job.get("preset_title") or local.stem
        return
    cmd = ytdlp_cmd()
    if not cmd:
        raise StepError("yt-dlp no esta instalado. Corre: pip install yt-dlp")
    VIDEOS.mkdir(exist_ok=True)
    args = [*cmd, "-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4",
            "--no-playlist", "-o", str(VIDEOS / "%(title).80B.%(ext)s"),
            "-j", "--no-simulate", "--no-warnings"]
    ff = find_tool("ffmpeg")
    if ff:
        args += ["--ffmpeg-location", str(Path(ff).parent)]
    out = run([*args, "--", url], job=job).stdout
    info = json.loads(out.strip().splitlines()[-1])
    path = (info.get("requested_downloads") or [{}])[0].get("filepath") or info.get("_filename")
    if not path or not Path(path).is_file():
        raise StepError("yt-dlp termino pero no encontre el archivo descargado.")
    job["files"]["raw"] = path
    job["title"] = info.get("title") or Path(path).stem
    job["source_description"] = (info.get("description") or "").strip()


def silence_cuts(path, duration):
    proc = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-i", str(path), "-vn", "-af",
         f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    silences, start = [], None
    for line in proc.stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif m := re.search(r"silence_end: (-?[\d.]+)", line):
            if start is not None:
                silences.append((start, float(m.group(1))))
                start = None
    if start is not None:
        silences.append((start, duration))
    cuts = [(s + SILENCE_PAD, e - SILENCE_PAD) for s, e in silences if e - s - 2 * SILENCE_PAD >= 0.2]
    keep, pos = [], 0.0
    for cs, ce in cuts:
        if cs - pos > 0.05:
            keep.append((pos, cs))
        pos = ce
    if duration - pos > 0.05:
        keep.append((pos, duration))
    return keep, sum(ce - cs for cs, ce in cuts)


def clean(job, workdir):
    raw = Path(job["files"]["raw"])
    out = workdir / "limpio.mp4"
    duration, has_audio = probe(raw)
    if not has_audio:
        log(job, "El video no tiene audio: no hay nada que limpiar.")
        shutil.copyfile(raw, out)
    else:
        keep, removed = silence_cuts(raw, duration)
        if not keep:
            raise StepError("Todo el audio es silencio: no queda nada que conservar.")
        if removed > 0:
            expr = "+".join(f"between(t,{a:.3f},{b + 1 if b >= duration else b:.3f})" for a, b in keep)
            graph = (f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB[v];"
                     f"[0:a]aselect='{expr}',asetpts=N/SR/TB,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
            (workdir / "limpiar.txt").write_text(graph, encoding="utf-8")
            ffmpeg(["-i", str(raw), "-/filter_complex", "limpiar.txt", "-map", "[v]", "-map", "[a]",
                    *venc(), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], cwd=workdir, job=job)
            log(job, f"Silencios quitados: {removed:.1f}s")
        else:
            ffmpeg(["-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], cwd=workdir, job=job)
            log(job, "No habia silencios largos; solo se empareja el volumen.")
    job["files"]["clean"] = str(out)
    job["duration"] = round(probe(out)[0], 1)


def plain_text(text):
    text = re.sub(r"[^\w\s.,!?¡¿'\-:%$#&()]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title, description=""):
    title = plain_text(re.sub(r"^video by\s+\S+", "", title, flags=re.I).strip(" -_."))
    if len(title) < 4 and description:
        title = plain_text(description.splitlines()[0])[:60]
    return title if len(title) >= 4 else "Nuevo clip"


def edit(job, workdir):
    # El fondo borroso se calcula en miniatura y se amplia: se ve igual y es ~4x mas rapido.
    graph = ("[0:v]split[a][b];"
             "[a]scale=108:192:force_original_aspect_ratio=increase,crop=108:192,boxblur=3:1,"
             "scale=1080:1920:flags=bilinear[bg];"
             "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
    (workdir / "editar.txt").write_text(graph, encoding="utf-8")
    out = workdir / "editado.mp4"
    ffmpeg(["-i", job["files"]["clean"], "-t", str(MAX_SECONDS), "-/filter_complex", "editar.txt",
            "-map", "[v]", "-map", "0:a?", *venc(), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], cwd=workdir, job=job)
    return out


def enhance(job, workdir):
    out = workdir / "mejorado.mp4"
    ffmpeg(["-i", str(job["files"]["edited"]),
            "-vf", "eq=contrast=1.06:saturation=1.15,unsharp=5:5:0.6:5:5:0.0",
            *venc(18), "-c:a", "copy", "-movflags", "+faststart", str(out)], cwd=workdir, job=job)
    return out


def brand_video(job, workdir):
    """Agente Marca: pone tu logo sobre el video ya mejorado. Sin logo guardado no hace nada."""
    if not marca.has_logo():
        return False
    out = workdir / "marcado.mp4"
    (workdir / "marcar.txt").write_text(marca.overlay_graph(), encoding="utf-8")
    ffmpeg(["-i", str(job["files"]["enhanced"]), "-i", str(marca.LOGO), "-/filter_complex", "marcar.txt",
            "-map", "[v]", "-map", "0:a?", *venc(18), "-c:a", "copy", "-movflags", "+faststart", str(out)],
           cwd=workdir, job=job)
    with lock:
        job["files"]["enhanced"] = str(out)
    return True


# ---------- estacion 5: traducir ----------
def prepare_speech(job, workdir):
    """Transcribe, traduce los segmentos y genera la voz doblada. No depende del titulo,
    por eso corre por adelantado, mientras tu apruebas."""
    try:
        lang, segments = ia.transcribe(find_tool("ffmpeg"), job["files"]["clean"], MAX_SECONDS)
        stt = {"src": lang, "speech": bool(segments), "segments": {}, "tts": {}}
        for code, _ in LANGS:
            if not segments:
                break
            if code == lang:
                stt["segments"][code] = segments
                continue
            texts = ia.translate_many([s["text"] for s in segments], lang, code)
            stt["segments"][code] = [{**s, "text": t} for s, t in zip(segments, texts)]
            paths = ia.speak_many([(s["text"], code, workdir / f"voz_{code}_{i}")
                                   for i, s in enumerate(stt["segments"][code])])
            stt["tts"][code] = [str(p) for p in paths]
    except ia.IAError as exc:
        raise StepError(str(exc))
    with lock:
        job["stt"] = stt
    save()


def translate_job(job):
    stt = job["stt"]
    title = job["title"]
    desc = (job.get("source_description", "").splitlines() or [""])[0][:140].strip() or title
    speech = stt["speech"]
    src = stt["src"] if speech else ia.guess_lang(f"{title} {desc}")
    if speech:
        log(job, f"Idioma detectado: {src}. {len(stt['segments'][src])} segmentos de voz.")
    else:
        log(job, "No se detecto voz: solo se traducen el titulo y la descripcion.")
    langs = {}
    try:
        for code, _ in LANGS:
            t, d = ia.translate_many([title, desc], src, code, workers=2)
            langs[code] = {"segments": stt["segments"].get(code, []), "title": t, "description": d}
    except ia.IAError as exc:
        raise StepError(str(exc))
    job["tr"] = {"src": src, "speech": speech, "langs": langs}


# ---------- estacion 6: voz + subtitulos + titulo ----------
def safe_name(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "_")[:50] or "clip"


def pick_font():
    for name in ("impact.ttf", "arialbd.ttf", "arial.ttf"):
        if (Path("C:/Windows/Fonts") / name).is_file():
            return name
    return None


def dub_inputs(job, workdir, code, duration):
    """Devuelve (archivos de voz, retardos en ms, tempos) para colocar cada frase en su tiempo."""
    segs = job["tr"]["langs"][code]["segments"]
    files = [Path(p) for p in job["stt"]["tts"][code]]
    delays, tempos = [], []
    for i, (seg, path) in enumerate(zip(segs, files)):
        tts_len, _ = probe(path)
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else duration
        slot = max(nxt - seg["start"], 0.5)
        delays.append(int(seg["start"] * 1000))
        tempos.append(min(max(tts_len / slot, 1.0), 1.6))
    return files, delays, tempos


def render_version(job, workdir, code):
    tr = job["tr"]
    data = tr["langs"][code]
    src_video = job["files"]["enhanced"]
    info = probe_full(src_video)
    duration = info["duration"]
    dubbed = tr["speech"] and code != tr["src"]

    (workdir / f"titulo_{code}.txt").write_text(
        "\n".join(textwrap.wrap(plain_text(data["title"]).upper() or "CLIP", width=18)[:3]), encoding="utf-8")
    font = pick_font()
    fontopt = f"fontfile='C\\:/Windows/Fonts/{font}':" if font else ""
    vf = ""
    if tr["speech"] and ia.write_srt(data["segments"], workdir / f"sub_{code}.srt"):
        vf += (f"subtitles=sub_{code}.srt:force_style='FontName=Arial,FontSize=10,Bold=1,Outline=3,Shadow=0,"
               "Alignment=2,MarginV=45',")
    vf += (f"drawtext={fontopt}textfile=titulo_{code}.txt:expansion=none:fontcolor=white:fontsize=84:borderw=5:"
           "bordercolor=0xFF2E88:x=(w-text_w)/2:y=140:line_spacing=10")
    graph = f"[0:v]{vf}[v]"
    inputs = ["-i", src_video]
    audio_args = ["-map", "0:a?", "-c:a", "copy"]
    if dubbed:
        files, delays, tempos = dub_inputs(job, workdir, code, duration)
        parts, labels = [], []
        if info["has_audio"]:
            parts.append(f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={DUCK_VOLUME}[bg]")
            labels.append("[bg]")
        for i, (f, d, t) in enumerate(zip(files, delays, tempos)):
            inputs += ["-i", str(f)]
            parts.append(f"[{i + 1}:a]aformat=sample_rates=44100:channel_layouts=stereo,atempo={t:.3f},"
                         f"adelay={d}:all=1[d{i}]")
            labels.append(f"[d{i}]")
        parts.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                     "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
        graph += ";" + ";".join(parts)
        audio_args = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{duration:.2f}"]
    (workdir / f"voz_{code}.txt").write_text(graph, encoding="utf-8")
    CLIPS.mkdir(exist_ok=True)
    out = CLIPS / f"{job['id']}_{code}_{safe_name(data['title'])}.mp4"
    ffmpeg([*inputs, "-/filter_complex", f"voz_{code}.txt", "-map", "[v]", *audio_args,
            *venc(18), "-movflags", "+faststart", str(out)], cwd=workdir, job=job)
    with lock:
        job["versions"][code] = {"file": out.name, "dubbed": dubbed, "title": data["title"],
                                 "description": data["description"], "hashtags": []}
    with contextlib.suppress(Exception):
        ensure_preview(out)
    log(job, f"Version {code.upper()} lista" + (" (doblada)." if dubbed else " (audio original)."))


# ---------- estacion 7: marketing ----------
STOPWORDS = {"this", "that", "with", "from", "have", "para", "como", "pero", "esto", "esta", "video", "your",
             "what", "when", "they", "them", "will", "were", "been", "tengo", "tiene", "estan", "porque"}


def market(job):
    """Agente Marketing: la IA escribe el titulo de publicacion, la descripcion con llamada a la accion,
    los hashtags y un primer comentario, cada idioma escrito de forma nativa (no traducido)."""
    handle = marca.load()["handle"]
    reason = (job.get("viral") or {}).get("reason", "")

    def one(code):
        v = job["versions"][code]
        transcript = " ".join(s["text"] for s in job["tr"]["langs"][code]["segments"])
        return code, cerebro.marketing_kit(code, v["title"], transcript, v["description"], handle, reason)

    with ThreadPoolExecutor(len(LANGS)) as pool:
        results = list(pool.map(lambda lang: one(lang[0]), LANGS))
    for code, kit in results:
        v = job["versions"][code]
        v["post_title"] = kit["post_title"] or v["title"]
        v["description"] = kit["description"] or v["description"]
        if handle:
            v["description"] = re.sub(r"@\w+", handle, v["description"])
            if handle.lower() not in v["description"].lower():
                v["description"] += f"\n{handle}"
        v["hashtags"] = kit["hashtags"]
        v["kit"] = {"first_comment": kit["first_comment"], "best_time": kit["best_time"], "ai": kit["ai"]}
        (CLIPS / v["file"]).with_suffix(".txt").write_text(
            f"{v['post_title']}\n\n{v['description']}\n\n{' '.join(v['hashtags'])}\n\n"
            f"Primer comentario: {kit['first_comment']}\nMejor hora: {kit['best_time']}\n", encoding="utf-8")


# ---------- estacion 8: check-in ----------
def checkin(job):
    checks = []

    def ck(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    ck("Confirmaste que el video es tuyo o tienes derechos", job["owner"],
       "" if job["owner"] else "Marca la casilla 'Es mi video' y procesa de nuevo.")
    ck("Existen las 2 versiones (Espanol e Ingles)",
       all(c in job["versions"] and (CLIPS / job["versions"][c]["file"]).is_file() for c, _ in LANGS))
    for code, label in LANGS:
        v = job["versions"].get(code)
        if not v or not (CLIPS / v["file"]).is_file():
            continue
        path = CLIPS / v["file"]
        try:
            info = probe_full(path)
            decode = subprocess.run([find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-f", "null", "-"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            vol = subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect",
                                  "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            m = re.search(r"mean_volume: (-?[\d.]+) dB", vol.stderr)
            mean = float(m.group(1)) if m else None
            ck(f"[{label}] El video se reproduce sin errores", not decode.stderr.strip(), decode.stderr.strip()[:120])
            ck(f"[{label}] Formato vertical 1080x1920", (info["width"], info["height"]) == (1080, 1920),
               f"{info['width']}x{info['height']}")
            ck(f"[{label}] Duracion maxima {MAX_SECONDS}s", info["duration"] <= MAX_SECONDS + 0.5,
               f"{info['duration']:.1f}s")
            ck(f"[{label}] Tiene audio con volumen audible", info["has_audio"] and mean is not None and mean > -45,
               "sin audio" if not info["has_audio"] else f"volumen medio {mean} dB")
        except Exception as exc:
            ck(f"[{label}] Revision tecnica", False, str(exc)[:150])
        ck(f"[{label}] Titulo (max 100 caracteres) y descripcion", 0 < len(v["title"]) <= 100 and bool(v["description"]),
           f"{len(v['title'])} caracteres")
        ck(f"[{label}] Hashtags", len(v["hashtags"]) >= 3)
    if job["tr"] and job["tr"]["speech"]:
        other = next((c for c, _ in LANGS if c != job["tr"]["src"]), None)
        same = job["tr"]["langs"]["es"]["title"].strip().lower() == job["tr"]["langs"]["en"]["title"].strip().lower()
        ck("Las traducciones son distintas entre si", other is not None and not same,
           "Titulo igual en ambos idiomas: revisa la traduccion.", required=False)
    ck("Hay al menos una cuenta vinculada para publicar", bool(publicar.linked_networks()),
       "Inicia sesion en una red en 'Cuentas' para poder subir.", required=False)
    viral = job.get("viral")
    if viral:
        ck("Puntaje viral de 50 o mas", viral["score"] >= 50, f"{viral['score']}/100: {viral['reason']}",
           required=False)
    ck("Tu logo esta en el video", marca.has_logo(), "Sube tu logo en 'Mi logo' para ganar seguidores.",
       required=False)
    return checks


# ---------- fases ----------
PRE = {}


class Prefetch:
    """Adelanta el trabajo que no depende de tu aprobacion (formato 9:16, mejora, transcripcion,
    traduccion de las frases y voz) para que, al aprobar, solo falte lo que depende del titulo."""
    STAGES = ("edit", "enhance", "brand", "speech")

    def __init__(self, job, workdir):
        self.job = job
        self.events = {k: threading.Event() for k in self.STAGES}
        self.done = set()
        self.error = None
        # El video (9:16 + mejora) y la voz (transcribir, traducir, doblar) solo necesitan el audio limpio,
        # asi que corren a la vez.
        threading.Thread(target=self.run_video, args=(job, workdir), daemon=True).start()
        threading.Thread(target=self.run_speech, args=(job, workdir), daemon=True).start()

    def finish(self, stage):
        self.done.add(stage)
        self.events[stage].set()

    def abort(self):
        for ev in self.events.values():
            ev.set()

    def fail(self, job, exc):
        self.error = self.error or str(exc)
        log(job, f"ERROR en el trabajo adelantado - {exc}")
        self.abort()

    def run_video(self, job, workdir):
        try:
            t = time.time()
            out = edit(job, workdir)
            with lock:
                job["files"]["edited"] = str(out)
            log(job, f"Formato 9:16 listo ({time.time() - t:.1f}s)")
            self.finish("edit")
            t = time.time()
            out = enhance(job, workdir)
            with lock:
                job["files"]["enhanced"] = str(out)
                job["duration"] = round(probe(out)[0], 1)
            log(job, f"Color y nitidez listos ({time.time() - t:.1f}s)")
            self.finish("enhance")
            t = time.time()
            if brand_video(job, workdir):
                log(job, f"Logo puesto ({time.time() - t:.1f}s)")
            self.finish("brand")
        except Cancelled:
            self.abort()
        except Exception as exc:
            self.fail(job, exc)

    def run_speech(self, job, workdir):
        try:
            t = time.time()
            prepare_speech(job, workdir)
            log(job, f"Transcripcion, traduccion y voz listas ({time.time() - t:.1f}s)")
            self.finish("speech")
        except Cancelled:
            self.abort()
        except Exception as exc:
            self.fail(job, exc)

    def wait(self, stage):
        self.events[stage].wait()
        if stage not in self.done:
            if self.job.get("cancelled"):
                raise Cancelled()
            raise StepError(self.error or "El trabajo adelantado fallo.")


def start_prefetch(job):
    pre = PRE.get(job["id"])
    if pre is None:
        pre = PRE[job["id"]] = Prefetch(job, WORK / job["id"])
    return pre


# ---------- Cazador viral: elige el mejor momento ----------
SCOUT_LENGTH = 45
SCOUT_MIN_SCORE = 25


def ai_hooks(job):
    """Permite cancelar el analisis de IA matando sus procesos y consultando si el trabajo se cancelo."""
    def on_proc(proc):
        with lock:
            PROCS.setdefault(job["id"], set()).add(proc)
        if job.get("cancelled"):
            proc.kill()
    return on_proc, (lambda: bool(job.get("cancelled")))


def cut_clip(src, start, end, out, job=None):
    ffmpeg(["-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}", "-map", "0:v:0", "-map", "0:a:0?",
            *venc(), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], job=job)


def viral_public(best):
    keys = ("start", "end", "score", "hook", "reason", "signals", "model", "lang", "text")
    return {k: best.get(k) for k in keys}


def fetch_reactions(job, workdir):
    """Chat, mapa de calor y comentarios con minuto del video original (solo si viene de un enlace)."""
    url = str(job.get("url", ""))
    if Path(url.strip('"')).is_file():
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    on_proc, stop = ai_hooks(job)
    log(job, "Buscando las reacciones de la audiencia (chat, mapa de calor y comentarios con minuto)...")
    try:
        react = reaccion.fetch(url, ytdlp_cmd(), workdir, on_proc, stop, log=lambda msg: log(job, msg))
    except InterruptedError:
        raise Cancelled()
    except Exception:
        react = None
    log(job, ("Reacciones encontradas: " + reaccion.summary(react)) if react else
        "Este video no publica datos de reacciones: se usa audio, imagen y texto.")
    return react


def scout_job(job, workdir):
    if job.get("viral"):
        v = job["viral"]
        log(job, f"Momento elegido por la IA: puntaje viral {v['score']}/100. {v['reason']}")
        return
    ff = find_tool("ffmpeg")
    on_proc, stop = ai_hooks(job)
    raw = Path(job["files"]["raw"])
    duration = probe(raw)[0]
    try:
        react = fetch_reactions(job, workdir)
        db = cerebro.audio_db(ff, raw, on_proc=on_proc, stop=stop)
        if duration > MAX_SECONDS + 5:
            log(job, "Analizando el audio y la imagen de todo el video para encontrar el mejor momento...")
            best = cerebro.pick_best(ff, raw, db, 0, duration, SCOUT_LENGTH, 3, job["title"], on_proc, stop, react)
            if not best:
                raise StepError("No encontre un momento con audio para elegir.")
            out = workdir / "seleccion.mp4"
            cut_clip(raw, best["start"], best["end"], out, job)
            with lock:
                job["files"]["source_full"] = str(raw)
                job["files"]["raw"] = str(out)
        else:
            cands = cerebro.window_candidates(db, 0, duration, duration, 1) or [{"start": 0, "end": duration}]
            best = cerebro.evaluate(ff, raw, {**cands[0], "start": 0, "end": duration}, job["title"],
                                    on_proc=on_proc, stop=stop, react=react)
    except cerebro.Stopped:
        raise Cancelled()
    except ia.IAError as exc:
        raise StepError(str(exc))
    with lock:
        job["viral"] = viral_public(best)
    log(job, f"Puntaje viral {best['score']}/100"
             + (f" (IA {best['model']})" if best["model"] else " (analisis basico, Ollama apagado)")
             + f". {best['reason']}")


def drop_source(job):
    """Los cortes que genera la app (clips de directos y mejores momentos) ya no hacen falta tras limpiar el audio."""
    raw = Path(job["files"].get("raw", ""))
    with contextlib.suppress(OSError):
        if raw.parent == VIDEOS and re.match(r"directo_|.+_viral\d+\.mp4$", raw.name):
            raw.unlink(missing_ok=True)


def phase1(job_id):
    job = get_job(job_id)
    if not job or job.get("cancelled"):
        return
    workdir = WORK / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        if "raw" not in job["files"]:
            with step(job, "receiver", "Bajando el video..."):
                receive(job)
        with step(job, "scout", "Cazador viral: buscando el mejor momento..."):
            scout_job(job, workdir)
        with step(job, "cleaner", "Limpiando audio (silencios + volumen)..."):
            clean(job, workdir)
        drop_source(job)
        set_agent(job, "editor", "working")
        hook = (job.get("viral") or {}).get("hook") or ""
        with lock:
            job["proposal"] = clean_title(hook or job["title"], job.get("source_description", ""))
            job["title"] = job["proposal"]
        start_prefetch(job)
        if not job.get("cancelled"):
            log(job, f"Titulo: {job['proposal']}")
            tasks.put(("phase2", job["id"], job["proposal"]))
    except Exception:
        pass


def phase2(job_id, title):
    job = get_job(job_id)
    if not job or job.get("cancelled"):
        return
    workdir = WORK / job["id"]
    try:
        pre = start_prefetch(job)
        with step(job, "editor", "Aplicando formato vertical 9:16..."):
            pre.wait("edit")
        with step(job, "enhancer", "Mejorando color y nitidez..."):
            pre.wait("enhance")
        with step(job, "brand", "Poniendo tu logo en el video..."):
            pre.wait("brand")
            if not marca.has_logo():
                log(job, "No hay logo guardado: sube el tuyo en 'Mi logo' para ganar seguidores.")
        with step(job, "translator", "Traduciendo titulo y descripcion a Espanol e Ingles..."):
            pre.wait("speech")
            translate_job(job)
        with step(job, "voice", "Armando voz, subtitulos y titulo de cada version..."):
            with ThreadPoolExecutor(len(LANGS)) as pool:
                for future in [pool.submit(render_version, job, workdir, code) for code, _ in LANGS]:
                    future.result()
        with step(job, "marketing", "Generando descripcion y hashtags por idioma..."):
            market(job)
        set_agent(job, "checkin", "working")
        with lock:
            job["checkin"] = {"state": "checking", "checks": []}
        log(job, "Check-in: revisando que todo este correcto...")
        checks = checkin(job)
        if job.get("cancelled"):
            return
        failed = [c for c in checks if c["required"] and not c["ok"]]
        with lock:
            job["checkin"] = {"state": "red" if failed else "green", "checks": checks}
        if failed:
            set_agent(job, "checkin", "failed")
            set_status(job, "blocked")
            with lock:
                job["error"] = "Check-in no aprobo: " + "; ".join(
                    f"{c['name']} ({c['detail']})" if c["detail"] else c["name"] for c in failed)
            log(job, "Luz ROJA. " + job["error"])
            return
        set_agent(job, "checkin", "done")
        set_status(job, "ready")
        log(job, "Luz VERDE: todo esta correcto para subir.")
        if job["auto"] and publicar.linked_networks():
            tasks.put(("phase3", job["id"]))
        elif job["auto"]:
            log(job, "No has iniciado sesion en ninguna red: inicia sesion en 'Cuentas' y pulsa Publicar.")
    except Exception:
        pass
    finally:
        PRE.pop(job_id, None)
        if job["status"] in ("ready", "blocked"):
            shutil.rmtree(workdir, ignore_errors=True)   # los archivos intermedios ya no se necesitan


def phase3(job_id):
    job = get_job(job_id)
    if not job or job.get("cancelled"):
        return
    try:
        set_status(job, "processing")
        with lock:
            job["error"] = ""
        failures = []
        with step(job, "publisher", "Publicando en tus redes vinculadas..."):
            nets = [n for n in publicar.linked_networks() if job.get("pub_nets") is None or n in job["pub_nets"]]
            if not nets:
                raise StepError("Para subir videos debes iniciar sesion en una red. Hazlo en 'Cuentas'.")
            dest = PUBLISHED / job["id"]
            dest.mkdir(parents=True, exist_ok=True)
            for code, _ in LANGS:
                v = job["versions"][code]
                shutil.copyfile(CLIPS / v["file"], dest / v["file"])
                shutil.copyfile((CLIPS / v["file"]).with_suffix(".txt"), dest / f"descripcion_{code}.txt")
            for net in nets:
                for code, _ in LANGS:
                    if job.get("cancelled"):
                        raise Cancelled()
                    if any(r["net"] == net and r["lang"] == code and r["ok"] for r in job["publish"]):
                        continue
                    v = job["versions"][code]
                    name = publicar.NETWORKS[net]["name"]
                    try:
                        url = publicar.publish(net, CLIPS / v["file"], v.get("post_title") or v["title"],
                                               v["description"], v["hashtags"], code)
                        result = {"net": net, "lang": code, "ok": True, "url": url, "error": ""}
                        log(job, f"Publicado en {name} ({code.upper()}): {url}")
                    except Exception as exc:
                        result = {"net": net, "lang": code, "ok": False, "url": "", "error": str(exc)[:300]}
                        failures.append(f"{name} {code.upper()}: {exc}")
                        log(job, f"ERROR en {name} ({code.upper()}): {exc}")
                    with lock:
                        job["publish"] = [r for r in job["publish"] if not (r["net"] == net and r["lang"] == code)]
                        job["publish"].append(result)
                    save()
            if not any(r["ok"] for r in job["publish"]):
                raise StepError("No se publico nada. " + " | ".join(failures)[:500])
        set_status(job, "published")
        with lock:
            job["error"] = ("Algunas publicaciones fallaron: " + " | ".join(failures)[:500]) if failures else ""
        save()
    except Exception:
        pass


def phase_split(job_id):
    """Modo automatico de la pantalla principal: la IA recorre el video por bloques de 3 minutos y saca el
    mejor momento de cada bloque. Corre en su propio hilo para que los clips ya elegidos se procesen a la vez."""
    job = get_job(job_id)
    if not job or job.get("cancelled"):
        return
    try:
        with step(job, "receiver", "Bajando el video para elegir sus mejores momentos..."):
            receive(job)
        raw = Path(job["files"]["raw"])
        duration, _ = probe(raw)
        if duration <= SPLIT_EVERY + 10:
            log(job, "El video dura menos de 3 minutos: se elige un solo mejor momento.")
            with lock:
                job["split"] = False
            tasks.put(("phase1", job["id"]))
            return
        ff = find_tool("ffmpeg")
        on_proc, stop = ai_hooks(job)
        base = clean_title(job["title"], job.get("source_description", ""))
        stem = safe_name(base)
        VIDEOS.mkdir(exist_ok=True)
        sent = 0
        with step(job, "scout", "Cazador viral: analizando el audio de todo el video..."):
            react = fetch_reactions(job, WORK / job["id"])
            db = cerebro.audio_db(ff, raw, on_proc=on_proc, stop=stop)
            blocks = [(a, min(a + SPLIT_EVERY, duration)) for a in range(0, int(duration), SPLIT_EVERY)]
            blocks = [b for b in blocks if b[1] - b[0] >= 15][:SPLIT_MAX]
            log(job, f"{len(blocks)} bloques de {SPLIT_EVERY // 60} min: elijo el mejor momento de cada uno.")
            for i, (a, b) in enumerate(blocks, 1):
                if job.get("cancelled"):
                    raise Cancelled()
                best = cerebro.pick_best(ff, raw, db, a, b, SCOUT_LENGTH, 2, base, on_proc, stop, react)
                if not best or best["score"] < SCOUT_MIN_SCORE:
                    log(job, f"Bloque {i}/{len(blocks)}: sin momento destacado, lo salto.")
                    continue
                sent += 1
                out = VIDEOS / f"{stem}_viral{sent:02d}.mp4"
                cut_clip(raw, best["start"], best["end"], out, job)
                submit_job(str(out), job["owner"], job["auto"], title=best["hook"] or f"{base} ({sent})",
                           description=job.get("source_description", ""), viral=viral_public(best))
                log(job, f"Bloque {i}/{len(blocks)}: clip {sent} elegido (puntaje {best['score']}) "
                         "y enviado a los agentes.")
        with lock:
            if job in jobs:
                jobs.remove(job)
        save()
    except Exception:
        pass


def worker():
    phases = {"phase1": phase1, "phase2": phase2, "phase3": phase3}
    while True:
        kind, *args = tasks.get()
        try:
            phases[kind](*args)
        finally:
            tasks.task_done()


# ---------- acciones de la API ----------
def submit_job(url, owner, auto, title=None, description="", live=None, split=False, viral=None):
    job = new_job(url, owner, auto, live)
    if description:
        job["source_description"] = description
    if title:
        job["preset_title"] = title
    job["split"] = split
    job["viral"] = viral
    if split:
        threading.Thread(target=phase_split, args=(job["id"],), daemon=True).start()
    else:
        tasks.put(("phase1", job["id"]))
    return job


def api_process(body):
    url = str(body.get("url", "")).strip()
    if not url:
        return 400, {"error": "Pega el enlace de tu video."}
    if not (url.lower().startswith(("http://", "https://")) or Path(url.strip('"')).is_file()):
        return 400, {"error": "Eso no parece un enlace ni un archivo existente."}
    job = submit_job(url, body.get("owner"), body.get("auto"), split=bool(body.get("split")))
    return 200, {"id": job["id"]}


def api_cancel(body):
    job = get_job(body.get("id"))
    if not job:
        return 404, {"error": "Ese video ya no esta en la cola."}
    if job["status"] != "processing":
        return 409, {"error": "Ese video ya termino. Usa 'Quitar' para sacarlo de la cola."}
    cancel_job(job)
    pre = PRE.pop(job["id"], None)
    if pre:
        pre.abort()
    return 200, {"ok": True}


def api_remove(body):
    job = get_job(body.get("id"))
    if not job:
        return 404, {"error": "Ese video ya no esta en la cola."}
    if job["status"] == "processing":
        return 409, {"error": "Cancela primero ese video."}
    with lock:
        jobs.remove(job)
    PRE.pop(job["id"], None)
    shutil.rmtree(WORK / job["id"], ignore_errors=True)
    save()
    return 200, {"ok": True}


def api_publish(body):
    with lock:
        job = get_job(body.get("id")) or next((j for j in jobs if j["status"] == "ready"), None)
    if not job or job["checkin"]["state"] != "green" or job["status"] not in ("ready", "published", "failed"):
        return 409, {"error": "No hay un clip aprobado por el Check-in. Procesa un video o espera la luz verde."}
    nets = body.get("nets")
    if nets is not None:
        nets = [n for n in nets if n in publicar.linked_networks()] if isinstance(nets, list) else None
    if not (publicar.linked_networks() if nets is None else nets):
        return 409, {"error": "Elige al menos una red con sesion iniciada. Inicia sesion en 'Cuentas'."}
    with lock:
        job["pub_nets"] = nets
    tasks.put(("phase3", job["id"]))
    return 200, {"ok": True}


def api_account_save(body):
    net = body.get("net")
    if net not in publicar.NETWORKS:
        return 404, {"error": "Red desconocida."}
    try:
        publicar.save_fields(net, body.get("fields") or {})
    except publicar.PublishError as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True}


def api_account_connect(body):
    net = body.get("net")
    if net not in publicar.NETWORKS or not publicar.NETWORKS[net]["oauth"]:
        return 404, {"error": "Esta red no usa conexion OAuth."}
    try:
        publicar.save_fields(net, body.get("fields") or {})
        return 200, {"url": publicar.start_oauth(net)}
    except publicar.PublishError as exc:
        return 400, {"error": str(exc)}


def api_account_unlink(body):
    net = body.get("net")
    if net not in publicar.NETWORKS:
        return 404, {"error": "Red desconocida."}
    publicar.unlink(net)
    return 200, {"ok": True}


def api_live_start(body):
    url = str(body.get("url", "")).strip()
    if not url.lower().startswith(("http://", "https://", "rtmp://", "rtmps://", "rtsp://")):
        return 400, {"error": "Pega el enlace del directo (YouTube, Twitch, Kick, un .m3u8, etc.)."}
    exe = find_tool("ffmpeg")
    if not exe:
        return 400, {"error": "ffmpeg no esta instalado."}
    sid = time.strftime("%H%M%S")
    while sid in LIVES:
        sid += "x"
    session = envivo.LiveSession(sid, url, WORK, exe, ytdlp_cmd(), VIDEOS, live_clip_job, {
        key: body.get(key) for key in ("mode", "every", "length", "auto", "owner") if key in body})
    session.can_clip = lambda: live_can_clip(sid)
    session.pick = live_pick
    with lock:
        LIVES[sid] = session
    session.start()
    return 200, {"id": sid}


def api_live_clip(body):
    session = LIVES.get(body.get("id"))
    if not session:
        return 404, {"error": "Ese directo ya no esta en la lista."}
    if session.status not in ("live", "ended"):
        return 409, {"error": "El directo todavia no esta grabando."}
    try:
        return 200, {"job": session.clip(int(body.get("seconds") or session.length))}
    except (envivo.LiveError, ValueError) as exc:
        return 400, {"error": str(exc)}


def api_live_settings(body):
    session = LIVES.get(body.get("id"))
    if not session:
        return 404, {"error": "Ese directo ya no esta en la lista."}
    with session.lock:
        if body.get("mode") in ("manual", "interval", "cycle", "peaks"):
            session.mode = body["mode"]
            session.last_clip_at = time.time()
            session.mark = session.total
        with contextlib.suppress(TypeError, ValueError):
            if "every" in body:
                session.every = max(envivo.AUTO_MIN_EVERY, int(body["every"]))
            if "length" in body:
                session.length = min(envivo.MAX_CLIP_SECONDS, max(10, int(body["length"])))
        for key in ("auto", "owner"):
            if key in body:
                setattr(session, key, bool(body[key]))
    return 200, {"ok": True}


def api_live_stop(body):
    session = LIVES.get(body.get("id"))
    if not session:
        return 404, {"error": "Ese directo ya no esta en la lista."}
    session.stop()
    return 200, {"ok": True}


def api_live_remove(body):
    with lock:
        session = LIVES.pop(body.get("id"), None)
    if not session:
        return 404, {"error": "Ese directo ya no esta en la lista."}
    threading.Thread(target=session.cleanup, daemon=True).start()
    return 200, {"ok": True}


MAX_LIVE_BACKLOG = 3


def live_can_clip(sid):
    """Frena los clips automaticos si hay demasiados en proceso o si los ultimos 3 fallaron."""
    with lock:
        mine = [j for j in jobs if j.get("live") == sid]
        if sum(j["status"] == "processing" for j in mine) >= MAX_LIVE_BACKLOG:
            return False
        if len(mine) >= 3 and all(j["status"] == "failed" for j in mine[:3]):
            session = LIVES.get(sid)
            if session and not session.error:
                session.error = ("Clips automaticos en pausa: los ultimos 3 fallaron. Revisa el aviso, "
                                 "quita los clips fallidos y se reanudan solos.")
            return False
        session = LIVES.get(sid)
        if session and session.error.startswith("Clips automaticos en pausa"):
            session.error = ""
    return True


def live_pick(window, session):
    """Modo automatico del directo: la IA elige el mejor momento de la ventana y lo corta."""
    ff = find_tool("ffmpeg")
    db = cerebro.audio_db(ff, window)
    seconds = len(db) * cerebro.HOP
    react = session.chat.curve(seconds) if session.chat else None
    best = cerebro.pick_best(ff, window, db, 0, seconds, min(float(session.length), seconds), 2, session.title,
                             react=react)
    if not best:
        return None
    VIDEOS.mkdir(exist_ok=True)
    out = VIDEOS / f"directo_{session.id}_{time.strftime('%H%M%S')}.mp4"
    cut_clip(window, best["start"], best["end"], out)
    job = submit_job(str(out), session.owner, session.auto, title=best["hook"] or plain_text(session.title)
                     or "Clip en vivo", description=session.title, live=session.id, viral=viral_public(best))
    log(job, f"La IA eligio este momento del directo (puntaje viral {best['score']}/100).")
    return job["id"]


def live_clip_job(path, session):
    title = plain_text(session.title) or "Clip en vivo"
    job = submit_job(str(path), session.owner, session.auto, title=title, description=session.title,
                     live=session.id)
    log(job, f"Clip creado del directo: {session.title or session.url}")
    return job["id"]


def public_job(job):
    out = {k: job.get(k) for k in ("id", "title", "status", "duration", "error", "proposal", "owner", "auto",
                                   "checkin", "publish", "agents", "log", "live", "viral")}
    out["versions"] = {code: {"video": f"/media/{quote(v['file'])}", "preview": f"/media/{quote(v['file'])}?preview=1",
                              "dubbed": v["dubbed"], "title": v["title"], "post_title": v.get("post_title", ""),
                              "description": v["description"], "hashtags": v["hashtags"], "kit": v.get("kit")}
                       for code, v in job["versions"].items()}
    return out


PREVIEWS = CLIPS / "previa"
PREVIEW_LOCK = threading.Lock()


def ensure_preview(original):
    """Copia para ver en el navegador: mismo video, audio MP3. Algunos navegadores (por ejemplo el integrado
    de VS Code) no decodifican AAC y se ven sin sonido. El archivo que se publica sigue siendo el original."""
    out = PREVIEWS / original.name
    with PREVIEW_LOCK:
        if out.is_file() and out.stat().st_mtime >= original.stat().st_mtime:
            return out
        PREVIEWS.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        ffmpeg(["-i", str(original), "-c:v", "copy", "-c:a", "libmp3lame", "-b:a", "160k", "-ar", "44100",
                "-movflags", "+faststart", str(tmp)])
        tmp.replace(out)
    return out


def api_brand_logo(body):
    exe = find_tool("ffmpeg")
    if not exe:
        return 400, {"error": "ffmpeg no esta instalado."}
    try:
        marca.save_logo(exe, body.get("data"))
    except marca.BrandError as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True}


def api_brand_settings(body):
    try:
        marca.save_settings(body)
    except marca.BrandError as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True}


def api_brand_clear(body):
    marca.clear_logo()
    return 200, {"ok": True}


def api_radar_save(body):
    try:
        radar.save_settings(body)
    except radar.RadarError as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True}


def api_radar_scan(body):
    if not radar.scan_now():
        return 409, {"error": "Ya hay una busqueda en marcha."}
    return 200, {"ok": True}


def api_radar_test(body):
    try:
        radar.send_test()
    except radar.RadarError as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True}


def api_radar_clear(body):
    radar.clear_found()
    return 200, {"ok": True}


def busy():
    with lock:
        return (any(j["status"] == "processing" for j in jobs)
                or any(s.status in ("connecting", "live") for s in LIVES.values()))


def api_bye(body):
    """La ventana se cerro: el programa termina cuando nadie vuelve a pedir nada y no queda trabajo en marcha."""
    mark = time.time()

    def later():
        if not CLOSE_HOOK or LAST_REQUEST[0] > mark:
            return   # la ventana volvio (recarga) o no es el programa de escritorio
        if time.time() < KEEPALIVE_UNTIL[0] or busy():
            threading.Timer(30, later).start()   # hay clips procesandose o directos grabando: se espera
            return
        CLOSE_HOOK()
    threading.Timer(8, later).start()
    return 200, {"ok": True}


def api_keepalive(body):
    """El panel va a ir a la pagina de una red para iniciar sesion: no cierres el programa mientras tanto."""
    KEEPALIVE_UNTIL[0] = time.time() + 1800
    return 200, {"ok": True}


def api_state():
    ai = cerebro.status()
    with lock:
        return {
            "tools": tools_status(),
            "agents": [{"k": a[0], "n": a[1], "r": a[2], "c": a[3]} for a in AGENTS],
            "accounts": publicar.summary(),
            "jobs": [public_job(j) for j in jobs],
            "lives": [s.to_dict() for s in LIVES.values()],
            "brand": marca.public(),
            "radar": radar.public(),
            "ai": ai,
        }


# ---------- HTTP ----------
LAST_REQUEST = [time.time()]
KEEPALIVE_UNTIL = [0.0]
CLOSE_HOOK = None   # lo pone app.py: cierra el programa cuando se cierra su ventana
OAUTH_PAGE = """<!doctype html><meta charset="utf-8"><title>Clip Agentes</title>
<body style="font-family:system-ui;background:#0a0612;color:#f5eefd;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center;max-width:460px"><h2>{title}</h2><p>{msg}</p><p>Ya puedes cerrar esta pestana.</p></div>
{script}</body>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ClipAgentes"

    def log_message(self, *args):
        pass

    def host_ok(self):
        return (self.headers.get("Host") or "").lower() in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def send_bytes(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self):
        LAST_REQUEST[0] = time.time()
        if not self.host_ok():
            return self.send_json(403, {"error": "host no permitido"})
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self.send_bytes(200, PANEL.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(200, api_state())
        elif path == "/brand/logo.png" and marca.has_logo():
            self.send_bytes(200, marca.LOGO.read_bytes(), "image/png")
        elif path == "/vendor/hls.min.js":
            self.send_bytes(200, (rutas.RES / "vendor" / "hls.min.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path.startswith("/live/"):
            self.serve_live(path[len("/live/"):])
        elif path.startswith("/media/"):
            query = parse_qs(parsed.query)
            self.serve_media(unquote(path[len("/media/"):]), "download" in query, "preview" in query)
        elif path.startswith("/oauth/"):
            self.oauth_callback(path[len("/oauth/"):], parse_qs(parsed.query))
        else:
            self.send_json(404, {"error": "no encontrado"})

    def oauth_callback(self, net, query):
        def page(title, msg, ok=False):
            back = "<script>setTimeout(()=>(window.close(),location.href='/'),2000)</script>" if ok else ""
            html = OAUTH_PAGE.format(title=title, msg=msg, script=back).encode("utf-8")
            self.send_bytes(200, html, "text/html; charset=utf-8")
        if net not in publicar.NETWORKS:
            return page("Red desconocida", "")
        if query.get("error"):
            return page("No se vinculo", f"La red respondio: {query['error'][0]}")
        try:
            publicar.finish_oauth(net, (query.get("code") or [""])[0], (query.get("state") or [""])[0])
        except Exception as exc:
            return page("No se pudo vincular", str(exc).replace("<", "&lt;"))
        page("Cuenta vinculada", f"{publicar.NETWORKS[net]['name']} quedo conectada. Volviendo al panel...", ok=True)

    def serve_live(self, rest):
        sid, _, name = rest.partition("/")
        session = LIVES.get(sid)
        if not session or not re.fullmatch(r"live\.m3u8|p\d+_\d{5}\.ts", name):
            return self.send_json(404, {"error": "no encontrado"})
        target = session.dir / "hls" / name
        try:
            data = target.read_bytes()
        except OSError:
            return self.send_json(404, {"error": "no encontrado"})
        self.send_bytes(200, data, "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t")

    def serve_media(self, name, download, preview=False):
        target = (CLIPS / name).resolve()
        if target.parent != CLIPS.resolve() or target.suffix != ".mp4" or not target.is_file():
            return self.send_json(404, {"error": "no encontrado"})
        if preview:
            try:
                target = ensure_preview(target)
            except StepError as exc:
                return self.send_json(500, {"error": str(exc)})
        size = target.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if m := re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "")):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def do_POST(self):
        LAST_REQUEST[0] = time.time()
        if not self.host_ok():
            return self.send_json(403, {"error": "host no permitido"})
        routes = {"/api/process": api_process,
                  "/api/cancel": api_cancel, "/api/remove": api_remove,
                  "/api/publish": api_publish, "/api/accounts/save": api_account_save,
                  "/api/accounts/connect": api_account_connect, "/api/accounts/unlink": api_account_unlink,
                  "/api/live/start": api_live_start, "/api/live/clip": api_live_clip,
                  "/api/live/settings": api_live_settings, "/api/live/stop": api_live_stop,
                  "/api/live/remove": api_live_remove, "/api/brand/logo": api_brand_logo,
                  "/api/brand/settings": api_brand_settings, "/api/brand/clear": api_brand_clear,
                  "/api/radar/save": api_radar_save, "/api/radar/scan": api_radar_scan,
                  "/api/radar/test": api_radar_test, "/api/radar/clear": api_radar_clear,
                  "/api/bye": api_bye, "/api/keepalive": api_keepalive}
        handler = routes.get(urlparse(self.path).path)
        if not handler:
            return self.send_json(404, {"error": "no encontrado"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json(415, {"error": "se espera JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 14 * 1024 * 1024:
                return self.send_json(413, {"error": "El archivo es demasiado grande."})
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self.send_json(400, {"error": "JSON invalido"})
        status, payload = handler(body)
        self.send_json(status, payload)


def kill_orphan_recorders():
    """Si el servidor se cerro a la fuerza, puede haber grabaciones de directos huerfanas: las cierra."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
              "Where-Object { $_.CommandLine -like '*seg*_%06d.ts*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    with contextlib.suppress(Exception):
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=20)


def stop_all_lives():
    for session in list(LIVES.values()):
        with contextlib.suppress(Exception):
            session.stop()


def warm_up():
    """Tareas lentas de arranque: el panel ya responde mientras se hacen."""
    kill_orphan_recorders()
    ia.preload()
    with contextlib.suppress(Exception):
        cerebro.llm_json("Reply with JSON.", '{"ok": 1}', max_tokens=5)
    for name, ok in tools_status().items():
        print(f"{name}: {'OK' if ok else 'FALTA'}")
    print(f"codificador de video: {hw_encoder() or 'procesador (libx264)'}")


def main():
    for folder in (VIDEOS, CLIPS, WORK, PUBLISHED):
        folder.mkdir(exist_ok=True)
    atexit.register(stop_all_lives)
    load()
    threading.Thread(target=worker, daemon=True).start()
    radar.configure(ytdlp_cmd(), find_tool("ffmpeg"))
    if publicar.ACCOUNTS_FILE.is_file():
        publicar._save(publicar._load())   # cifra los archivos guardados por versiones anteriores
    if radar.FILE.is_file():
        radar._save(radar._load())
    radar.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=warm_up, daemon=True).start()
    print(f"Panel en http://127.0.0.1:{PORT}  (Ctrl+C para salir)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
