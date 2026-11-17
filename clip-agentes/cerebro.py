"""Cerebro: elige los mejores momentos de un video y arma el marketing.

Todo corre en tu PC. El analisis de audio e imagen usa ffmpeg, la transcripcion usa Whisper y el
juicio editorial (gancho, puntaje viral, titulos, descripcion, hashtags) lo hace un modelo local de
Ollama. Si Ollama no esta encendido, se usa un analisis basico sin IA generativa.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import ia

OLLAMA = os.environ.get("CLIP_OLLAMA_URL", "http://127.0.0.1:11434")
PREFERRED = ("qwen2.5:14b", "qwen2.5:7b", "qwen3:8b", "llama3.1:8b", "gemma3:12b", "gemma2:9b", "mistral", "llama3.2")
LANG_NAMES = {"es": "Spanish", "en": "English"}
HOP = 0.5

_llm_lock = threading.Lock()
_cache = {"t": 0.0, "model": None}
last_error = ""


class Stopped(Exception):
    """El trabajo se cancelo mientras se analizaba."""


# ---------- modelo local ----------
def _ollama(path, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def llm_model():
    """Nombre del mejor modelo instalado en Ollama, o None si no esta disponible."""
    if os.environ.get("CLIP_LLM"):
        return os.environ["CLIP_LLM"]
    if time.time() - _cache["t"] < 20:
        return _cache["model"]
    model = None
    try:
        names = [m["name"] for m in _ollama("/api/tags", timeout=3).get("models", [])]
        for pref in PREFERRED:
            model = next((n for n in names if n.startswith(pref)), None)
            if model:
                break
        model = model or next((n for n in names if "embed" not in n), None)
    except (OSError, ValueError, urllib.error.URLError):
        model = None
    _cache.update(t=time.time(), model=model)
    return model


def status():
    model = llm_model()
    return {"ok": bool(model), "model": model or ""}


def llm_json(system, user, max_tokens=450, timeout=150, temperature=0.5):
    """Pide una respuesta JSON al modelo local. Devuelve un dict o None."""
    global last_error
    model = llm_model()
    if not model:
        return None
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "20m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": max_tokens}}
    try:
        with _llm_lock:
            reply = _ollama("/api/chat", body, timeout=timeout)
        text = reply.get("message", {}).get("content", "")
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        last_error = str(exc)[:200]
        _cache["t"] = 0.0
        return None


# ---------- audio: energia por medio segundo ----------
def audio_db(ffmpeg, path, start=0.0, length=None, on_proc=None, stop=None):
    """Nivel de sonido (dB) cada HOP segundos."""
    import numpy as np
    cmd = [ffmpeg, "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(path)]
    if length:
        cmd += ["-t", str(length)]
    cmd += ["-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if on_proc:
        on_proc(proc)
    n = int(8000 * HOP)
    frame_bytes = n * 4
    out, buf = [], b""
    try:
        while True:
            chunk = proc.stdout.read(frame_bytes * 600)
            if not chunk:
                break
            buf += chunk
            usable = len(buf) // frame_bytes * frame_bytes
            if usable:
                arr = np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, n)
                out.append(20 * np.log10(np.sqrt((arr ** 2).mean(axis=1)) + 1e-9))
                buf = buf[usable:]
            if stop and stop():
                proc.kill()
                raise Stopped()
    finally:
        proc.stdout.close()
        proc.wait()
    if stop and stop():
        raise Stopped()
    return np.concatenate(out) if out else np.zeros(0)


def window_candidates(db, a, b, length, count, step=2.0):
    """Mejores ventanas de `length` s entre a y b segun sonido: volumen, contraste, voz y picos."""
    import numpy as np
    n = int(length / HOP)
    i0, i1 = int(a / HOP), min(int(b / HOP), len(db)) if len(db) else int(b / HOP)
    if len(db) == 0:
        # Sin audio: se reparten ventanas por el bloque y las separa la puntuacion de imagen.
        span = max(0.0, (b - a) - length)
        if b - a < 8:
            return []
        return [{"start": a + span * k / max(count, 1), "end": min(a + span * k / max(count, 1) + length, b),
                 "audio": 0.0} for k in range(count)]
    if i1 - i0 < int(8 / HOP):
        return []
    n = min(n, i1 - i0)
    med, sd = float(np.median(db)), max(float(db.std()), 1.0)
    floor = float(np.percentile(db, 10))
    active = (db > floor + 10).astype(float)
    csum, asum = np.cumsum(np.insert(db, 0, 0.0)), np.cumsum(np.insert(active, 0, 0.0))
    found = []
    for i in range(i0, i1 - n + 1, max(1, int(step / HOP))):
        seg = db[i:i + n]
        mean = (csum[i + n] - csum[i]) / n
        act = (asum[i + n] - asum[i]) / n
        dyn = float(np.percentile(seg, 90) - np.percentile(seg, 30))
        peak = float(seg.max())
        loud = min(max(0.5 + (mean - med) / (3 * sd), 0), 1)
        score = 100 * (0.40 * loud + 0.25 * min(dyn / 25, 1) + 0.25 * act
                       + 0.10 * min(max(0.5 + (peak - med) / (4 * sd), 0), 1))
        if act < 0.30:
            score *= 0.5
        found.append((score, i * HOP))
    found.sort(reverse=True)
    picked = []
    for score, start in found:
        if all(abs(start - p["start"]) >= length * 0.8 for p in picked):
            picked.append({"start": start, "end": min(start + length, b), "audio": round(score, 1)})
        if len(picked) >= count:
            break
    return picked


# ---------- imagen: cortes de escena ----------
def visual_score(ffmpeg, path, start, length, on_proc=None):
    cmd = [ffmpeg, "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path), "-an",
           "-vf", "fps=4,scale=160:-2,select='gt(scene,0.15)',showinfo", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    if on_proc:
        on_proc(proc)
    _, err = proc.communicate()
    cuts = err.count("pts_time:")
    return round(min(100.0, cuts / max(length / 10, 1) * 33), 1)


# ---------- texto ----------
HOOKS = {
    "es": ("increible", "increíble", "nunca", "jamas", "jamás", "secreto", "error", "verdad", "mira", "cuidado",
           "gratis", "imposible", "locura", "nadie", "todos", "millones", "dinero", "truco", "ojo", "wow",
           "por que", "por qué", "como", "cómo", "ganar", "perder", "peor", "mejor", "primera vez", "no puedo"),
    "en": ("incredible", "never", "secret", "mistake", "truth", "look", "careful", "free", "impossible", "insane",
           "nobody", "everyone", "million", "money", "trick", "wow", "why", "how", "win", "lose", "worst", "best",
           "first time", "can't believe", "crazy"),
}


def text_score(text, duration, lang):
    words = re.findall(r"\w+", text.lower())
    if not words:
        return 0.0
    hooks = HOOKS.get(lang, HOOKS["es"])
    low = text.lower()
    head = " ".join(low.split()[:14])
    wps = len(words) / max(duration, 1)
    score = 0
    score += 15 if ("?" in text or "¿" in text) else 0
    score += 10 if re.search(r"\d", text) else 0
    score += min(15, 5 * text.count("!"))
    score += min(30, 8 * sum(h in low for h in hooks))
    score += 20 if 2.0 <= wps <= 3.8 else (8 if wps > 1.2 else 0)
    score += 15 if ("?" in head or "¿" in head or any(h in head for h in hooks)) else 0
    score += 10 if len(words) >= 25 else 0
    return float(min(100, score))


def llm_rate(text, lang, source_title):
    name = LANG_NAMES.get(lang, "Spanish")
    system = ("You are an expert short-form video editor for TikTok, Instagram Reels and YouTube Shorts. "
              "You judge whether a clip hooks viewers in the first seconds, keeps attention and gets shared. "
              "Be strict: most clips deserve 30-65, only exceptional ones go above 80. Reply ONLY with JSON.")
    user = (f"Transcript of a candidate clip (language: {name}):\n\"\"\"{text[:1800]}\"\"\"\n"
            f"Original video title: {source_title or 'unknown'}\n\n"
            f"Return JSON with these keys: \"score\" (integer 0-100), "
            f"\"hook\" (on-screen title in {name}, max 55 characters, bold claim or curiosity gap, no hashtags, "
            f"no emojis, no quotes), \"reason\" (one short sentence in Spanish explaining the score).")
    data = llm_json(system, user, max_tokens=220)
    if not isinstance(data, dict):
        return None
    try:
        score = max(0, min(100, int(float(data.get("score")))))
    except (TypeError, ValueError):
        return None
    hook = re.sub(r"[\"'#*_`]", "", str(data.get("hook", ""))).strip()[:60]
    return {"score": score, "hook": hook, "reason": str(data.get("reason", "")).strip()[:200]}


# ---------- evaluar un momento ----------
def evaluate(ffmpeg, path, cand, source_title="", with_visual=True, on_proc=None, stop=None, react=None):
    """Transcribe y puntua un momento {start, end}. Devuelve el momento con su puntaje viral y su gancho.
    Si hay reacciones de la audiencia (react), pesan mas que todo lo demas: ese es el momento que la gente vivio."""
    start, end = float(cand["start"]), float(cand["end"])
    length = end - start
    try:
        lang, segs = ia.transcribe(ffmpeg, path, length, start=start)
    except ia.IAError:
        lang, segs = None, []   # sin pista de audio o ilegible: se evalua solo por sonido e imagen
    if stop and stop():
        raise Stopped()
    text = " ".join(s["text"] for s in segs)
    audio = float(cand.get("audio", 50.0))
    visual = visual_score(ffmpeg, path, start, length, on_proc) if with_visual else 0.0
    txt = text_score(text, length, lang or "es") if segs else 0.0
    rated = llm_rate(text, lang or "es", source_title) if segs and llm_model() else None
    reaction = react.score(start, end) if react else None
    if reaction is not None:
        if rated:
            score = 0.40 * reaction + 0.15 * audio + 0.10 * visual + 0.05 * txt + 0.30 * rated["score"]
        elif segs:
            score = 0.45 * reaction + 0.20 * audio + 0.10 * visual + 0.25 * txt
        else:
            score = 0.55 * reaction + 0.25 * audio + 0.20 * visual
    elif rated:
        score = 0.30 * audio + 0.15 * visual + 0.10 * txt + 0.45 * rated["score"]
    elif segs:
        score = 0.45 * audio + 0.25 * visual + 0.30 * txt
    else:
        score = 0.60 * audio + 0.40 * visual
    new_start, new_end = start, end
    if segs and reaction is None:
        s0, s1 = segs[0]["start"], segs[-1]["end"]
        if s1 - s0 >= 18:
            new_start = start + max(0.0, s0 - 0.4)
            new_end = min(end, start + s1 + 0.6)
    hook = (rated or {}).get("hook") or ""
    reason = (rated or {}).get("reason") or _basic_reason(audio, visual, txt, bool(segs))
    detail = react.detail(start, end) if react else ""
    if detail and reaction >= 25:
        reason = detail + ". " + reason
    return {"start": round(new_start, 2), "end": round(new_end, 2), "score": int(round(score)),
            "hook": hook, "reason": reason,
            "lang": lang, "text": text[:600],
            "signals": {"audio": int(audio), "visual": int(visual), "text": int(txt),
                        "ai": rated["score"] if rated else None,
                        "reaction": int(reaction) if reaction is not None else None},
            "model": llm_model() if rated else ""}


def _basic_reason(audio, visual, txt, speech):
    bits = []
    if audio >= 60:
        bits.append("energia de audio alta")
    if visual >= 50:
        bits.append("mucho movimiento y cortes")
    if txt >= 50:
        bits.append("texto con gancho")
    if not speech:
        bits.append("sin voz: elegido por sonido e imagen")
    return ("Analisis basico: " + ", ".join(bits)) if bits else "Analisis basico: momento sin picos claros."


def pick_best(ffmpeg, path, db, a, b, length=45, tries=2, source_title="", on_proc=None, stop=None, react=None):
    """El mejor momento entre a y b: preselecciona por reaccion de la audiencia y por sonido, luego evalua."""
    cands = window_candidates(db, a, b, length, tries)
    if react:
        for cand in react.windows(a, b, length, tries):
            audio = window_candidates(db, cand["start"], cand["end"], length, 1)
            cand["audio"] = audio[0]["audio"] if audio else 50.0
            cands.append(cand)
    if not cands:
        return None
    best = None
    for cand in cands:
        result = evaluate(ffmpeg, path, cand, source_title, on_proc=on_proc, stop=stop, react=react)
        if best is None or result["score"] > best["score"]:
            best = result
    return best


# ---------- marketing ----------
BASE_TAGS = ("shorts", "reels", "fyp")
STOP = {"this", "that", "with", "from", "have", "para", "como", "pero", "esto", "esta", "video", "your", "what",
        "when", "they", "them", "will", "were", "been", "tengo", "tiene", "estan", "porque", "para", "entre"}


def _clean_tags(tags, extra_words=""):
    out = []
    for t in tags:
        t = re.sub(r"[^\w]", "", str(t).lower(), flags=re.UNICODE)
        if 3 <= len(t) <= 30 and t not in out:
            out.append(t)
    for w in re.findall(r"[a-záéíóúñ0-9]{5,}", extra_words.lower()):
        if len(out) >= 6:
            break
        if w not in STOP and w not in out:
            out.append(w)
    out = out[:9]
    out += [t for t in BASE_TAGS if t not in out]
    return ["#" + t for t in out[:12]]


BEST_TIME = {"es": "Martes a jueves 18:00-21:00 o fines de semana 10:00-13:00 (hora de tu audiencia).",
             "en": "Tuesday to Thursday 6-9 pm or weekends 10 am-1 pm (your audience's time zone)."}


def marketing_kit(lang, title, transcript, source_desc, handle="", reason=""):
    """Titulo de publicacion, descripcion con llamada a la accion, hashtags y primer comentario."""
    name = LANG_NAMES.get(lang, "Spanish")
    system = (f"You are a world-class social media growth strategist for TikTok, Reels and YouTube Shorts. "
              f"You write scroll-stopping, honest copy that earns follows and comments. "
              f"Use ONLY facts that appear in the transcript: never invent numbers, names, games or claims. "
              f"Write EVERYTHING in {name} only. Reply ONLY with JSON.")
    user = (f"Write the post copy in {name} for this short clip.\n"
            f"On-screen title: {title}\nWhat is said in the clip: \"{transcript[:1200]}\"\n"
            f"Original description: {source_desc[:300] or 'none'}\nWhy it can work: {reason or 'n/a'}\n"
            f"Creator handle: {handle or 'none'}\n\n"
            "Return JSON with keys: \"post_title\" (max 70 chars, hook first, no hashtags), "
            "\"description\" (2-3 short lines, max 300 chars, ends with a call to action to follow"
            + (f" {handle}" if handle else "") + " and comment), "
            "\"hashtags\" (array of 8 words without #, all directly related to the transcript topic: "
            "3 broad, 3 niche, 2 trending-style), "
            "\"first_comment\" (one question that makes people reply, max 140 chars). "
            f"All text values must be in {name}.")
    data = llm_json(system, user, max_tokens=380)
    fallback_cta = {"es": "Sigueme para mas clips" + (f" {handle}" if handle else "") + " y comenta que opinas.",
                    "en": "Follow for more clips" + (f" {handle}" if handle else "") + " and tell me what you think."}
    if not isinstance(data, dict):
        return {"post_title": title[:70], "description": (source_desc[:200] + "\n" if source_desc else "")
                + fallback_cta.get(lang, fallback_cta["es"]),
                "hashtags": _clean_tags([], f"{title} {transcript}"), "first_comment": "",
                "best_time": BEST_TIME.get(lang, BEST_TIME["es"]), "ai": False}
    tags = data.get("hashtags") if isinstance(data.get("hashtags"), list) else []
    clean = lambda s: re.sub(r"[*_`]", "", str(s)).strip()
    return {"post_title": clean(data.get("post_title") or title)[:90],
            "description": clean(data.get("description") or fallback_cta.get(lang, ""))[:600],
            "hashtags": _clean_tags(tags, f"{title} {transcript}"),
            "first_comment": clean(data.get("first_comment", ""))[:200],
            "best_time": BEST_TIME.get(lang, BEST_TIME["es"]), "ai": True}
