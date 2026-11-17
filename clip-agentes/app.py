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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--ytdlp":
        return run_ytdlp()
    import rutas
    if rutas.FROZEN:
        hide_child_consoles()
    import publicar
    import server

    url = f"http://127.0.0.1:{publicar.PORT}"
    if not serving(url):
        threading.Thread(target=server.main, daemon=True).start()
        for _ in range(100):
            if serving(url):
                break
            time.sleep(0.2)
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        threading.Event().wait()   # sin Edge ni Chrome no hay ventana propia: queda abierto en el navegador
        return
    profile = rutas.DATA / "ventana"
    window = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                               "--window-size=1320,900"])
    window.wait()   # al cerrar la ventana se cierra el programa
    server.stop_all_lives()


if __name__ == "__main__":
    main()
