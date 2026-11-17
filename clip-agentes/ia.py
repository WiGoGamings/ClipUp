"""Transcripcion (Whisper local), traduccion y voz para los agentes."""
import asyncio
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import rutas

VOICES = {"es": "es-MX-JorgeNeural", "en": "en-US-AndrewNeural"}
SAPI_VOICES = {"es": "Microsoft Helena Desktop", "en": "Microsoft David Desktop"}
MYMEMORY_CODES = {"es": "es-ES", "en": "en-US"}

_model = None
_google_blocked_until = 0.0


class IAError(Exception):
    pass


_model_lock = threading.Lock()


def _whisper():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise IAError("Falta faster-whisper. Corre: pip install faster-whisper")
            threads = os.cpu_count() or 4
            bundled = rutas.APP_DIR / "modelos" / "whisper-base"   # viene dentro del instalador: funciona sin internet
            source = str(bundled) if bundled.is_dir() else "base"
            try:
                # Primero desde el cache local: evita consultar internet en cada arranque.
                _model = WhisperModel(source, device="cpu", compute_type="int8", cpu_threads=threads,
                                      local_files_only=True)
            except Exception:
                _model = WhisperModel(source, device="cpu", compute_type="int8", cpu_threads=threads)
        return _model


def preload():
    """Carga Whisper en segundo plano al iniciar, para que el primer clip no espere."""
    def go():
        try:
            _whisper()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def transcribe(ffmpeg_exe, video, max_seconds=None, start=0.0):
    """Devuelve (idioma, segmentos). El audio se decodifica con ffmpeg para no depender de PyAV."""
    try:
        import numpy as np
    except ImportError:
        raise IAError("Falta numpy. Corre: pip install faster-whisper")
    limit = ["-t", str(max_seconds)] if max_seconds else []
    seek = ["-ss", str(start)] if start else []
    proc = subprocess.run(
        [ffmpeg_exe, "-v", "error", *seek, "-i", str(video), *limit, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le",
         "-"], capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if "does not contain any stream" in err or "matches no streams" in err:
            return None, []   # el video no tiene pista de audio
        raise IAError("ffmpeg no pudo leer el audio: " + err[-200:])
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size < 8000:
        return None, []
    segments, info = _whisper().transcribe(audio, vad_filter=True)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
           for s in segments if s.text.strip()]
    return (info.language if out else None), out


def guess_lang(text):
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    es = sum(w in {"el", "la", "de", "que", "y", "en", "un", "para", "con", "los", "las", "es", "por"} for w in words)
    en = sum(w in {"the", "and", "to", "of", "is", "in", "for", "you", "with", "this", "that", "are"} for w in words)
    return "en" if en > es else "es"


def _google(text, src, dst):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source=src, target=dst).translate(text)


def _mymemory(text, src, dst):
    from deep_translator import MyMemoryTranslator
    out = MyMemoryTranslator(source=MYMEMORY_CODES.get(src, src), target=MYMEMORY_CODES.get(dst, dst)).translate(text)
    if not out or "MYMEMORY WARNING" in out.upper():
        raise IAError("limite diario de MyMemory alcanzado")
    return out


def translate(text, src, dst):
    global _google_blocked_until
    text = text.strip()
    if not text or src == dst:
        return text
    providers = [("MyMemory", _mymemory)]
    if time.time() > _google_blocked_until:
        providers.insert(0, ("Google", _google))
    errors = []
    for attempt in range(2):   # un corte de red momentaneo no debe tumbar todo el clip
        for name, fn in providers:
            try:
                return fn(text[:450], src, dst)
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:90]}")
                if name == "Google":
                    _google_blocked_until = time.time() + 600
        if attempt == 0:
            time.sleep(2)
    raise IAError("No pude traducir. " + " | ".join(errors[:2]))


def _srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def write_srt(segments, path, max_words=5):
    """Parte cada segmento en subtitulos cortos de hasta max_words palabras."""
    lines, n = [], 0
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        parts = math.ceil(len(words) / max_words)
        size = math.ceil(len(words) / parts)
        chunks = [words[i:i + size] for i in range(0, len(words), size)]
        total = sum(len(" ".join(c)) for c in chunks) or 1
        t, span = seg["start"], seg["end"] - seg["start"]
        for chunk in chunks:
            text = " ".join(chunk)
            end = t + span * len(text) / total
            n += 1
            lines.append(f"{n}\n{_srt_time(t)} --> {_srt_time(end)}\n{text}\n")
            t = end
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return n


async def _edge(text, voice, out):
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, voice).save(str(out)), timeout=45)


def _sapi(text, lang, out):
    txt = Path(out).with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SelectVoice('{SAPI_VOICES[lang]}'); $s.SetOutputToWaveFile('{out}'); "
              f"$s.Speak([IO.File]::ReadAllText('{txt}', [Text.Encoding]::UTF8)); $s.Dispose()")
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    txt.unlink(missing_ok=True)
    if proc.returncode != 0 or not Path(out).is_file():
        raise IAError("La voz de Windows fallo: " + (proc.stderr or "").strip()[-150:])


async def _edge_many(items):
    sem = asyncio.Semaphore(6)

    async def one(text, lang, out):
        async with sem:
            try:
                await _edge(text, VOICES[lang], out)
                return out.is_file() and out.stat().st_size > 0
            except Exception:
                return False

    return await asyncio.gather(*(one(t, l, o) for t, l, o in items))


def speak_many(jobs):
    """Genera varias voces a la vez. jobs = [(texto, idioma, ruta_base)]. Devuelve las rutas en el mismo orden.
    Usa edge-tts (neural) y, si falla o no hay internet, la voz offline de Windows."""
    items = [(t, l, Path(b).with_suffix(".mp3")) for t, l, b in jobs]
    try:
        ok = asyncio.run(_edge_many(items))
    except Exception:
        ok = [False] * len(items)
    paths = []
    for (text, lang, mp3), good in zip(items, ok):
        if good:
            paths.append(mp3)
            continue
        wav = mp3.with_suffix(".wav")
        _sapi(text, lang, wav)
        paths.append(wav)
    return paths


def translate_many(texts, src, dst, workers=3):
    """Traduce varios textos en paralelo, respetando el orden."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda t: translate(t, src, dst), texts))
