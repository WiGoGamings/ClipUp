"""Rutas de la app: donde estan sus recursos y donde guarda tus datos (cuentas, videos, clips)."""
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
CODE = Path(__file__).resolve().parent
# Recursos que van dentro del programa (panel, vendor) y carpeta del .exe (ffmpeg incluido).
RES = Path(getattr(sys, "_MEIPASS", CODE))
APP_DIR = Path(sys.executable).parent if FROZEN else CODE
# Instalado: los datos van a %APPDATA% (Program Files no se puede escribir). Desde el codigo: junto a el.
DATA = Path(os.environ.get("CLIP_DATA") or (Path(os.environ.get("APPDATA", Path.home())) / "ClipAgentes" if FROZEN else CODE))
DATA.mkdir(parents=True, exist_ok=True)
