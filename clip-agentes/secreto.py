"""Cifrado de secretos con la proteccion de Windows (DPAPI): solo tu usuario de esta PC puede abrirlos."""
import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def available():
    return sys.platform == "win32"


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn, data):
    src, _keep = _blob(data)
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI no pudo procesar el dato.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    """Texto -> 'dpapi:...'. Si DPAPI no existe (otro sistema), devuelve el texto tal cual."""
    if not available() or text.startswith(PREFIX):
        return text
    raw = _call(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
    return PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect(text):
    """'dpapi:...' -> texto. Lo que no este cifrado se devuelve igual (archivos de versiones anteriores)."""
    if not text.startswith(PREFIX):
        return text
    return _call(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(text[len(PREFIX):])).decode("utf-8")
