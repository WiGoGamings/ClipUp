"""Clip Agentes como programa de escritorio: arranca el servidor y abre el panel en su propia ventana.

Tambien hace de yt-dlp cuando el programa va empaquetado:  ClipAgentes.exe --ytdlp <argumentos>
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def run_ytdlp():
    import yt_dlp
    yt_dlp.main(sys.argv[2:])


def hide_child_consoles():
    """Sin consola propia, cada ffmpeg o yt-dlp que lanza el programa abriria una ventana negra."""
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched


def find_browser():
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            candidates += [Path(root) / "Microsoft/Edge/Application/msedge.exe",
                           Path(root) / "Google/Chrome/Application/chrome.exe"]
    return next((str(c) for c in candidates if c and Path(c).is_file()), None)


def serving(url):
    try:
        urllib.request.urlopen(url + "/api/state", timeout=2).close()
        return True
    except OSError:
        return False


def ensure_ollama():
    """Si la IA local (Ollama) esta instalada pero apagada, la enciende en segundo plano."""
    if serving("http://127.0.0.1:11434") or not shutil.which("ollama") and not OLLAMA_EXE.is_file():
        return
    exe = shutil.which("ollama") or str(OLLAMA_EXE)
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)


OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--ytdlp":
        return run_ytdlp()
    import rutas
    if rutas.FROZEN:
        hide_child_consoles()
        log = rutas.DATA / "registro.txt"
        if log.is_file() and log.stat().st_size > 2_000_000:
            log.unlink()
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")   # sin consola: los errores quedan aqui
    import publicar
    import server

    url = f"http://127.0.0.1:{publicar.PORT}"
    ensure_ollama()
    already_running = serving(url)
    if not already_running:
        threading.Thread(target=server.main, daemon=True).start()
        for _ in range(300):   # hasta 60 s: en un primer arranque Windows revisa los archivos nuevos
            if serving(url):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("El servidor no arranco en 60 segundos.")
    browser = find_browser()
    if browser:
        # No se espera a este proceso: si Edge ya estaba abierto con ese perfil, el nuevo proceso termina al
        # instante y se cerraria el programa. Al cerrar la ventana el panel avisa solo (ver /api/bye).
        subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={rutas.DATA / 'ventana'}", "--no-first-run",
                          "--window-size=1320,900"])
    else:
        webbrowser.open(url)
    if already_running:
        return   # ya habia otra copia funcionando: solo se abrio otra ventana

    def close():
        server.stop_all_lives()
        os._exit(0)
    server.CLOSE_HOOK = close
    threading.Event().wait()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        text = traceback.format_exc()
        print(text)
        if getattr(sys, "frozen", False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "Clip Agentes no pudo arrancar:\n\n" + text[-600:], "Clip Agentes", 0x10)
        raise
