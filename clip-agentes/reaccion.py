"""Reacciones de la audiencia: encuentra el segundo exacto en que mas gente reacciono.

Fuentes (las que existan para el video): chat del directo (mensajes, risas, donaciones), mapa de calor de
YouTube ("lo mas repetido") y comentarios que citan un minuto exacto ("3:25 jajaja").
"""
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

CHAT_SMOOTH = 12          # segundos que se suman para medir el pico de chat
CHAT_LAG = 5              # el chat reacciona unos segundos despues de lo que paso
PEAK_AT = 0.65            # el pico de reaccion cae al 65% del clip: antes va lo que la provoco
MIN_CHAT = 30
MIN_LIVE_CHAT = 15
MIN_STAMPS = 3
WEIGHTS = {"chat": 0.55, "heat": 0.25, "comments": 0.20}

LAUGH = re.compile(r"\b(?:ja|je|ji|jo|ha|he){2,}j?\b|\blo+l\b|lmao|lmfao|rofl|\bkek|\bxd+\b|😂|🤣|😹|"
                   r":face-with-tears-of-joy:|:rolling-on-the-floor-laughing:|:smiling-face-with-open-mouth", re.I)
HYPE = re.compile(r"\bwo+w+\b|\bomg\b|\bgg\b|\bpog|\bclip\b|no way|\bno+\b|dios|\bwtf\b|brutal|locura|"
                  r"increible|insane|crazy|😱|😮|🔥|💀|❤|👍|👏|:fire:|:red-heart:|:hundred-points:|:skull:|:clapping", re.I)
STAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
PAID = ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer", "liveChatMembershipItemRenderer",
        "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer")


def _run(cmd, on_proc=None, stop=None, timeout=900):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace")
    if on_proc:
        on_proc(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    if stop and stop():
        raise InterruptedError()
    return proc.returncode, out, err


def _moving(x, win):
    return np.convolve(x, np.ones(win), mode="same")


def _strength(series, win):
    """0-100: cuantas veces supera cada tramo a lo normal de este mismo video (1x = 0, 5x o mas = 100)."""
    s = _moving(series, win)
    base = max(float(np.median(s)), 0.25 * float(s.mean()), 1.0)
    return np.clip((s / base - 1) / 4, 0, 1) * 100, s, base


class Curve:
    def __init__(self, values, raw, stats, base):
        self.values, self.raw, self.stats, self.base = values, raw, stats, base

    def score(self, a, b):
        seg = self.values[max(0, int(a)):int(b)]
        return float(0.6 * seg.max() + 0.4 * seg.mean()) if seg.size else 0.0

    def windows(self, a, b, length, count):
        """Los `count` mejores clips de `length` s entre a y b, con el pico de reaccion hacia el final del clip."""
        v = self.values.copy()
        lo, hi = int(a), min(int(b), len(v))
        out = []
        mask = np.zeros(len(v), dtype=bool)
        mask[lo:hi] = True
        while len(out) < count and mask.any():
            masked = np.where(mask, v, -1)
            peak = int(masked.argmax())
            if masked[peak] <= 0:
                break
            start = min(max(peak - PEAK_AT * length, a), max(a, b - length))
            out.append({"start": float(start), "end": float(min(start + length, b)),
                        "reaction": round(self.score(start, start + length), 1), "peak": peak})
            mask[max(0, int(peak - length * 0.8)):int(peak + length * 0.8)] = False
        return out

    def detail(self, a, b):
        """Frase corta con lo que paso en ese tramo (para mostrarla al usuario)."""
        seg = self.values[max(0, int(a)):int(b)]
        if not seg.size:
            return ""
        peak = max(0, int(a)) + int(seg.argmax())
        lo, hi = max(0, peak - CHAT_SMOOTH // 2), peak + CHAT_SMOOTH // 2
        bits = []
        msgs = int(self.raw["msgs"][lo:hi].sum())
        if self.stats["chat"] and msgs:
            bits.append(f"chat {msgs} mensajes en {CHAT_SMOOTH} s ({msgs / self.base:.1f}x lo normal)")
            for key, word in (("laughs", "risas"), ("hype", "reacciones"), ("paid", "donaciones o membresias")):
                n = int(self.raw[key][lo:hi].sum())
                if n:
                    bits.append(f"{n} {word}")
        if self.stats["comments"]:
            n = int(self.raw["stamps"][max(0, peak - 8):peak + 8].sum())
            if n:
                bits.append(f"{n} comentarios citan este minuto")
        if self.stats["heatmap"] and self.raw["heat"][min(peak, len(self.raw["heat"]) - 1)] >= 0.6:
            bits.append("zona mas repetida del video")
        if not bits:
            return ""
        return f"La audiencia reacciono en {int(peak // 60)}:{int(peak % 60):02d} - " + ", ".join(bits)


def _msg_text(renderer):
    out = []
    for run in (renderer.get("message") or {}).get("runs", []):
        if "text" in run:
            out.append(run["text"])
        elif "emoji" in run:
            e = run["emoji"]
            out.append(e.get("emojiId") or (e.get("shortcuts") or [""])[0])
    return "".join(out)


def _count_item(item):
    """(mensaje, risa, reaccion, donacion) de un mensaje del chat, como 0/1."""
    text = item.get("liveChatTextMessageRenderer")
    if text:
        msg = _msg_text(text)
        return 1, int(bool(LAUGH.search(msg))), int(bool(HYPE.search(msg))), 0
    if any(k in item for k in PAID):
        return 0, 0, 0, 1
    return 0, 0, 0, 0


def _parse_chat(path, n):
    keys = ("msgs", "laughs", "hype", "paid")
    arr = {k: np.zeros(n) for k in keys}
    total = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                act = json.loads(line).get("replayChatItemAction") or {}
                sec = int(int(act.get("videoOffsetTimeMsec", 0)) / 1000)
            except (ValueError, AttributeError):
                continue
            sec = min(max(sec, 0), n - 1)
            for action in act.get("actions", []):
                counts = _count_item((action.get("addChatItemAction") or {}).get("item") or {})
                for key, c in zip(keys, counts):
                    arr[key][sec] += c
                total += counts[0]
    return arr, total


def _chat_curve(chat):
    """Curva 0-100 de la actividad del chat, adelantada CHAT_LAG s porque la gente reacciona despues."""
    activity = chat["msgs"] + 2 * chat["laughs"] + chat["hype"] + 8 * chat["paid"]
    activity = np.roll(activity, -CHAT_LAG)
    activity[-CHAT_LAG:] = 0
    values, _, _ = _strength(activity, CHAT_SMOOTH)
    return values, max(float(np.median(_moving(chat["msgs"], CHAT_SMOOTH))), 1.0)


class LiveChat:
    """Lee en segundo plano el chat de un directo de YouTube en curso y arma la curva de reaccion de una ventana."""

    def __init__(self, url, ytdlp, workdir):
        self.url, self.ytdlp, self.dir = url, ytdlp, Path(workdir) / "chat"
        self.events = {}   # segundo (reloj real) -> [mensajes, risas, reacciones, donaciones]
        self.done = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.proc = None

    def start(self):
        if not self.ytdlp or not re.match(r"https?://", self.url):
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [*self.ytdlp, "--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-warnings",
             "--no-playlist", "-o", str(self.dir / "c"), "--", self.url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _loop(self):
        while not self.stop_event.is_set():
            for frag in sorted(self.dir.glob("c.live_chat.json.part-Frag*")):
                if frag.name in self.done:
                    continue
                try:
                    data = json.loads(frag.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue   # todavia se esta escribiendo: se reintenta en la siguiente vuelta
                self.done.add(frag.name)
                actions = ((data.get("continuationContents") or {}).get("liveChatContinuation") or {}).get("actions", [])
                with self.lock:
                    for action in actions:
                        item = (action.get("addChatItemAction") or {}).get("item") or {}
                        counts = _count_item(item)
                        if not any(counts):
                            continue
                        renderer = next(iter(item.values()), {})
                        try:
                            sec = int(int(renderer["timestampUsec"]) / 1_000_000)
                        except (KeyError, ValueError, TypeError):
                            continue
                        ev = self.events.setdefault(sec, [0, 0, 0, 0])
                        for i, c in enumerate(counts):
                            ev[i] += c
                    old = time.time() - 7200
                    for sec in [s for s in self.events if s < old]:
                        del self.events[sec]
            self.stop_event.wait(5)

    def curve(self, seconds):
        """Curva de los ultimos `seconds` s (el final de la ventana es ahora), o None si el chat esta muy quieto."""
        n = int(seconds) + 1
        end = int(time.time())
        raw = {k: np.zeros(n) for k in ("msgs", "laughs", "hype", "paid", "stamps", "heat")}
        with self.lock:
            for sec, ev in self.events.items():
                i = sec - (end - n + 1)
                if 0 <= i < n:
                    for key, c in zip(("msgs", "laughs", "hype", "paid"), ev):
                        raw[key][i] += c
        total = int(raw["msgs"].sum())
        if total < MIN_LIVE_CHAT:
            return None
        values, base = _chat_curve(raw)
        return Curve(values, raw, {"chat": total, "heatmap": False, "comments": 0}, base)


def fetch(url, ytdlp, workdir=None, on_proc=None, stop=None, log=None):
    """Devuelve un Curve con la reaccion de la audiencia por segundo, o None si no hay datos de ese video."""
    say = log or (lambda msg: None)
    if not ytdlp or not re.match(r"https?://", str(url)):
        return None
    try:
        code, out, err = _run([*ytdlp, "-J", "--no-playlist", "--no-warnings", "--write-comments",
                               "--extractor-args", "youtube:max_comments=300,300,0,0;comment_sort=top",
                               "--", url], on_proc, stop, timeout=300)
        info = json.loads(out)
    except InterruptedError:
        raise
    except Exception:
        return None
    n = int(info.get("duration") or 0) + 1
    if n < 20:
        return None

    raw = {"msgs": np.zeros(n), "laughs": np.zeros(n), "hype": np.zeros(n), "paid": np.zeros(n),
           "stamps": np.zeros(n), "heat": np.zeros(n)}
    stats = {"chat": 0, "heatmap": False, "comments": 0}
    parts, base = {}, 1.0

    if "live_chat" in (info.get("subtitles") or {}):
        try:
            with tempfile.TemporaryDirectory(dir=workdir) as tmp:
                _run([*ytdlp, "--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-warnings",
                      "--no-playlist", "-o", str(Path(tmp) / "c"), "--", url], on_proc, stop, timeout=900)
                found = next(Path(tmp).glob("*.live_chat.json"), None)
                if found:
                    chat, total = _parse_chat(found, n)
                    if total >= MIN_CHAT:
                        raw.update(chat)
                        parts["chat"], base = _chat_curve(chat)
                        stats["chat"] = total
        except InterruptedError:
            raise
        except Exception as exc:
            say(f"No pude leer el chat del directo: {str(exc)[:80]}")

    heat = info.get("heatmap") or []
    if len(heat) >= 10:
        for h in heat:
            a, b = int(h.get("start_time", 0)), int(np.ceil(h.get("end_time", 0)))
            raw["heat"][a:b] = float(h.get("value", 0))
        med = float(np.median(raw["heat"]))
        parts["heat"] = np.clip((raw["heat"] - med) / max(1 - med, 0.05), 0, 1) * 100
        stats["heatmap"] = True

    stamps = 0
    for c in info.get("comments") or []:
        if c.get("parent") not in (None, "root"):
            continue
        weight = 1 + np.log10(1 + (c.get("like_count") or 0))
        for m in STAMP.finditer(c.get("text") or ""):
            sec = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if sec < n:
                raw["stamps"][sec] += weight
                stamps += 1
    if stamps >= MIN_STAMPS:
        s = _moving(raw["stamps"], 10)
        parts["comments"] = s / max(float(s.max()), 1e-9) * 100 * min(1.0, stamps / 8)
        stats["comments"] = stamps

    if not parts:
        return None
    total_w = sum(WEIGHTS[k] for k in parts)
    values = sum(parts[k] * WEIGHTS[k] for k in parts) / total_w
    return Curve(values, raw, stats, base)


def summary(curve):
    s = curve.stats
    bits = []
    if s["chat"]:
        bits.append(f"chat ({s['chat']} mensajes)")
    if s["heatmap"]:
        bits.append("mapa de calor de YouTube")
    if s["comments"]:
        bits.append(f"{s['comments']} comentarios con minuto")
    return ", ".join(bits)
