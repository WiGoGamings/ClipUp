"""Construye el programa para Windows: carpeta dist/ClipAgentes y, si hay Inno Setup, el instalador.

Uso:  python construir.py
Resultado: salida/ClipAgentes-Setup.exe  (y dist/ClipAgentes/ClipAgentes.exe, que tambien se puede usar suelto)
"""
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "1.0.0"
COLLECT = ["faster_whisper", "ctranslate2", "onnxruntime", "av", "yt_dlp", "edge_tts", "deep_translator",
           "tokenizers", "huggingface_hub"]


def sh(*cmd, **kw):
    subprocess.run([str(c) for c in cmd], check=True, cwd=HERE, **kw)


def make_icon(path):
    """Icono 256x256 (degradado rosa a cian con un triangulo de 'play'), sin depender de Pillow."""
    size = 256
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            t = (x + y) / (2 * size)
            r, g, b = int(255 * (1 - t) + 0 * t), int(46 * (1 - t) + 229 * t), int(136 * (1 - t) + 255 * t)
            inside = 88 <= x <= 88 + 96 * (1 - abs(y - 128) / 70) and abs(y - 128) <= 70
            row += bytes((255, 255, 255, 255) if inside else (r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    Path(path).write_bytes(header + png)


def find_ffmpeg():
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        if not found:
            root = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
            found = next((str(p) for p in root.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe")), None)
        if not found:
            sys.exit(f"No encontre {name}. Instala ffmpeg (winget install Gyan.FFmpeg) y vuelve a correr esto.")
        yield Path(found)


def find_inno():
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("PROGRAMFILES", "")):
        for sub in ("Programs/Inno Setup 6", "Inno Setup 6"):
            exe = Path(base) / sub / "ISCC.exe"
            if exe.is_file():
                return exe
    return shutil.which("iscc")


def main():
    icon = HERE / "icono.ico"
    if not icon.is_file():
        make_icon(icon)
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "ClipAgentes",
            "--icon", icon, "--add-data", "panel.html;.", "--add-data", "vendor;vendor"]
    for mod in COLLECT:
        args += ["--collect-all", mod]
    sh(*args, "app.py")

    target = HERE / "dist" / "ClipAgentes" / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    for exe in find_ffmpeg():
        shutil.copy2(exe, target / exe.name)

    iscc = find_inno()
    if not iscc:
        print("\nPrograma listo en dist/ClipAgentes. Para el instalador instala Inno Setup (winget install JRSoftware.InnoSetup).")
        return
    sh(iscc, f"/DVersion={VERSION}", "instalador.iss")
    print("\nInstalador listo: salida/ClipAgentes-Setup.exe")


if __name__ == "__main__":
    main()
