"""Grabacion de directos (streams) en tiempo real y creacion de clips.

Un directo se graba en segmentos de 10 s dentro de un buffer rotativo (los ultimos 15 minutos).
Un clip se arma uniendo los segmentos mas recientes, sin recodificar.
"""
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import reaccion

SEGMENT_SECONDS = 10
BUFFER_SECONDS = 900
MAX_CLIP_SECONDS = 58
MAX_RECORD_SECONDS = 4 * 3600
AUTO_EVERY = 180
AUTO_MIN_EVERY = 30
PEAK_DB_ABOVE_BASELINE = 6.0
PEAK_COOLDOWN = 90


class LiveError(Exception):
    pass


class LiveSession:
    def __init__(self, sid, url, base_dir, ffmpeg_exe, ytdlp_cmd, videos_dir, on_clip, options):
        self.id = sid
        self.url = url
        self.dir = Path(base_dir) / f"live_{sid}"
        self.ffmpeg = ffmpeg_exe
        self.ytdlp = ytdlp_cmd
        self.videos_dir = Path(videos_dir)
        self.on_clip = on_clip
        self.status = "connecting"
        self.title = ""
        self.error = ""
        self.segments = []
        self.clips = []
        self.mode = options.get("mode") or "interval"
        self.every = max(AUTO_MIN_EVERY, int(options.get("every") or AUTO_EVERY))
        self.length = min(MAX_CLIP_SECONDS, max(10, int(options.get("length") or 30)))
        self.auto = bool(options.get("auto"))
        self.owner = bool(options.get("owner"))
        self.last_clip_at = time.time()
        self.total = 0.0
        self.mark = 0.0
        self.run_no = 0
        self.seen = set()
        self.can_clip = lambda: True
        self.pick = None
        self.picking = False
        self.proc = None
        self.chat = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self._analyzed = 0
        self._baseline = None
        self._peak_at = 0.0

    # ---------- conexion ----------
    def start(self):
        if "youtu" in self.url.lower():
            self.chat = reaccion.LiveChat(self.url, self.ytdlp, self.dir)
            self.chat.start()
        threading.Thread(target=self._run, daemon=True).start()

    def _resolve(self):
        low = self.url.lower()
        if ".m3u8" in low or low.startswith(("rtmp://", "rtmps://", "rtsp://")):
            self.title = "Directo"
            return [self.url], {}
        if not self.ytdlp:
            raise LiveError("yt-dlp no esta instalado. Corre: pip install yt-dlp")
        proc = subprocess.run([*self.ytdlp, "-j", "--no-playlist", "--no-warnings",
                               "-f", "bv*[height<=1080]+ba/b[height<=1080]/b/best",
                               "--", self.url], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-4:])
            raise LiveError(tail or "No pude leer ese enlace.")
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        if not info.get("is_live"):
            raise LiveError("Ese enlace no es un directo en vivo. Para un video normal usa 'Procesar'.")
        self.title = info.get("title") or "Directo"
        # YouTube suele dar el video y el audio por separado: entonces son dos entradas para ffmpeg.
        urls = [f["url"] for f in (info.get("requested_formats") or []) if f.get("url")][:2] or [info.get("url")]
        if not urls[0]:
            raise LiveError("No encontre una URL de transmision para ese directo.")
        return urls, info.get("http_headers") or {}

    def _launch(self):
        stream, headers = self._resolve()
        if self.stop_event.is_set():
            return False
        self.run_no += 1
        n = self.run_no
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "hls").mkdir(exist_ok=True)
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for url in stream:
            if headers:
                args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
            if url.lower().startswith("http"):
                args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            args += ["-i", url]
        vmap, amap = "0:v:0?", ("1:a:0?" if len(stream) > 1 else "0:a:0?")
        args += ["-map", vmap, "-map", amap, "-c", "copy", "-t", str(MAX_RECORD_SECONDS),
                 "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
                 "-reset_timestamps", "1", "-segment_list", f"lista{n}.csv", "-segment_list_type", "csv",
                 f"seg{n}_%06d.ts",
                 "-map", vmap, "-map", amap, "-c:v", "copy", "-c:a", "libmp3lame", "-b:a", "128k",
                 "-ar", "44100", "-ac", "2", "-t", str(MAX_RECORD_SECONDS), "-f", "hls",
                 "-hls_time", "2", "-hls_list_size", "30",
                 "-hls_flags", "delete_segments+omit_endlist+independent_segments",
                 "-hls_segment_filename", f"hls/p{n}_%05d.ts", "hls/live.m3u8"]
        self.errlog = (self.dir / "ffmpeg.log").open("wb")
        self.proc = subprocess.Popen(args, cwd=self.dir, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self.errlog)
        return True

    def _run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                before = self.total
                if not self._launch():
                    return
                outcome = self._monitor()
                if outcome != "retry":
                    return
                # El directo se cayo a media grabacion: se reconecta (hasta 5 intentos seguidos sin datos nuevos).
                failures = failures + 1 if self.total == before else 0
                if failures >= 5:
                    self.status = "ended"
                    return
                self.error = ""
                self.stop_event.wait(3.0)
        except Exception as exc:
            if self.segments:
                self.status = "ended"
                self.error = f"El directo se corto: {str(exc)[:200]}"
            else:
                self.status = "error"
                self.error = str(exc)[:400]

    # ---------- vigilancia ----------
    def _read_segments(self):
        rows = []
        for listing in sorted(self.dir.glob("lista*.csv")):
            for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split(",")
                if len(parts) == 3:
                    try:
                        rows.append((parts[0], float(parts[1]), float(parts[2])))
                    except ValueError:
                        pass
        if not rows:
            return
        with self.lock:
            known = {s[0] for s in self.segments}
            gone = {s[0] for s in self.segments if not (self.dir / s[0]).is_file()}
            for name, start, end in rows:
                if name not in known and name not in self.seen and (self.dir / name).is_file():
                    self.segments.append((name, start, end - start))
                    self.seen.add(name)
                    self.total += end - start
            self.segments = [s for s in self.segments if s[0] not in gone]
            total = sum(s[2] for s in self.segments)
            while total > BUFFER_SECONDS and len(self.segments) > 1:
                name, _, dur = self.segments.pop(0)
                (self.dir / name).unlink(missing_ok=True)
                total -= dur
                self._analyzed = max(0, self._analyzed - 1)

    def recorded(self):
        with self.lock:
            return sum(s[2] for s in self.segments)

    def _monitor(self):
        while not self.stop_event.is_set():
            self._read_segments()
            if self.segments and self.status == "connecting":
                self.status = "live"
            if self.proc.poll() is not None:
                self._read_segments()
                self.errlog.close()
                if self.proc.returncode != 0 and self.segments:
                    return "retry"
                if self.segments:
                    self.status = "ended"
                else:
                    tail = (self.dir / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")
                    self.status = "error"
                    self.error = "\n".join(tail.strip().splitlines()[-4:]) or "No se pudo grabar el directo."
                return "done"
            try:
                self._auto()
            except Exception as exc:
                self.error = f"Auto-clip: {str(exc)[:200]}"
            self.stop_event.wait(1.0)
        return "stopped"

    def _segment_volume(self, name):
        out = subprocess.run([self.ffmpeg, "-hide_banner", "-i", str(self.dir / name), "-vn", "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"mean_volume: (-?[\d.]+) dB", out.stderr)
        return float(m.group(1)) if m else None

    def _window_file(self, seconds):
        """Une los ultimos `seconds` s grabados en un archivo temporal (sin recodificar) para analizarlos."""
        with self.lock:
            segs = list(self.segments)
            if not segs:
                raise LiveError("Aun no hay video grabado.")
            chosen, total = [], 0.0
            for seg in reversed(segs):
                chosen.insert(0, seg)
                total += seg[2]
                if total >= seconds:
                    break
            stamp = int(time.time() * 1000)
            listing = self.dir / f"win_{stamp}.txt"
            listing.write_text("".join(
                "file '" + (self.dir / s[0]).resolve().as_posix().replace("'", "'\\''") + "'\n" for s in chosen),
                encoding="utf-8")
        out = self.dir / f"win_{stamp}.mp4"
        proc = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                               "-i", str(listing), "-c", "copy", str(out)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        listing.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.is_file():
            raise LiveError("No pude armar la ventana de analisis.")
        return out

    def _pick_worker(self):
        """La IA elige el mejor momento de los ultimos minutos y lo convierte en clip."""
        try:
            job_id = None
            window = self._window_file(self.every + 5)
            try:
                if self.pick:
                    job_id = self.pick(window, self)
            finally:
                window.unlink(missing_ok=True)
            if job_id is None:
                job_id = self.clip(self.length)
            else:
                with self.lock:
                    self.clips.append(job_id)
                    self.last_clip_at = time.time()
        except Exception as exc:
            self.error = f"Auto-clip: {str(exc)[:200]}"
        finally:
            self.picking = False

    def _start_pick(self):
        self.picking = True
        self.mark = self.total
        threading.Thread(target=self._pick_worker, daemon=True).start()

    def _auto(self):
        now = time.time()
        if self.mode == "interval":
            if self.total - self.mark >= self.every and not self.picking and self.can_clip():
                self._start_pick()
        elif self.mode == "cycle":
            if self.total - self.mark >= self.length and self.can_clip():
                self.clip(self.length)
        elif self.mode == "peaks":
            with self.lock:
                pending = list(self.segments[self._analyzed:])
                self._analyzed = len(self.segments)
            for name, _, _ in pending:
                vol = self._segment_volume(name)
                if vol is None:
                    continue
                base = self._baseline
                self._baseline = vol if base is None else base * 0.9 + vol * 0.1
                if (base is not None and vol >= base + PEAK_DB_ABOVE_BASELINE and vol > -40
                        and now - self.last_clip_at >= PEAK_COOLDOWN and self.recorded() >= 20
                        and self.can_clip()):
                    self.clip(self.length)
                    break

    # ---------- clips ----------
    def clip(self, seconds):
        seconds = min(MAX_CLIP_SECONDS, max(10, int(seconds)))
        with self.lock:
            segs = list(self.segments)
            if not segs:
                raise LiveError("Aun no hay video grabado. Espera unos segundos.")
            chosen, total = [], 0.0
            for seg in reversed(segs):
                chosen.insert(0, seg)
                total += seg[2]
                if total >= seconds:
                    break
            while total > MAX_CLIP_SECONDS and len(chosen) > 1:
                total -= chosen.pop(0)[2]
            listing = self.dir / f"clip_{int(time.time())}.txt"
            listing.write_text("".join(
                "file '" + (self.dir / s[0]).resolve().as_posix().replace("'", "'\\''") + "'\n" for s in chosen),
                encoding="utf-8")
        self.videos_dir.mkdir(exist_ok=True)
        out = self.videos_dir / f"directo_{self.id}_{time.strftime('%H%M%S')}.mp4"
        proc = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                               "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        listing.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.is_file():
            raise LiveError("No pude armar el clip: " + "\n".join(proc.stderr.strip().splitlines()[-3:]))
        job_id = self.on_clip(out, self)
        with self.lock:
            self.clips.append(job_id)
            self.last_clip_at = time.time()
            self.mark = self.total

    # ---------- control ----------
    def stop(self):
        self.stop_event.set()
        if self.chat:
            self.chat.stop()
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if self.status in ("connecting", "live"):
            self.status = "stopped"

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def preview_ready(self):
        return self.status == "live" and (self.dir / "hls" / "live.m3u8").is_file()

    def to_dict(self):
        with self.lock:
            return {"id": self.id, "url": self.url, "title": self.title, "status": self.status,
                    "recorded": int(self.total), "clips": len(self.clips),
                    "mode": self.mode, "length": self.length, "every": self.every,
                    "next_in": (max(0, int((self.every if self.mode == "interval" else self.length)
                                           - (self.total - self.mark)))
                                if self.mode in ("interval", "cycle") else None),
                    "auto": self.auto, "owner": self.owner, "error": self.error, "picking": self.picking,
                    "preview": f"/live/{self.id}/live.m3u8" if self.preview_ready() else ""}
