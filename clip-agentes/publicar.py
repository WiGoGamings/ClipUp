"""Cuentas vinculadas y publicacion en redes (YouTube, TikTok, Instagram, Facebook).

Los datos de las cuentas se guardan solo en cuentas.json (en tu PC). Nunca se envian al navegador.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rutas
import secreto

BASE = rutas.DATA
ACCOUNTS_FILE = BASE / "cuentas.json"
GRAPH = "https://graph.facebook.com/v21.0"
PORT = 8765

NETWORKS = {
    "youtube": {
        "name": "YouTube Shorts",
        "pays": "Programa de socios de YouTube",
        "dev": "https://console.cloud.google.com/apis/credentials",
        "oauth": True,
        "fields": [
            {"key": "client_id", "label": "Client ID", "secret": False},
            {"key": "client_secret", "label": "Client secret", "secret": True},
        ],
        "help": "En console.cloud.google.com crea un proyecto, activa YouTube Data API v3 y crea credenciales "
                "OAuth tipo 'Aplicacion web' con esta URI de redireccion: {redirect}. Las apps sin verificar "
                "por Google suben los videos como PRIVADOS hasta que las verifiques.",
    },
    "tiktok": {
        "name": "TikTok",
        "pays": "Creator Rewards Program",
        "dev": "https://developers.tiktok.com/apps",
        "oauth": True,
        "fields": [
            {"key": "client_key", "label": "Client key", "secret": False},
            {"key": "client_secret", "label": "Client secret", "secret": True},
        ],
        "help": "En developers.tiktok.com crea una app, agrega 'Login Kit' y 'Content Posting API' y registra "
                "esta URI de redireccion: {redirect}. Hasta que TikTok audite tu app, solo puede publicar "
                "videos privados. El programa de pagos suele pedir videos de mas de 1 minuto.",
    },
    "instagram": {
        "name": "Instagram Reels",
        "pays": "Bonos de Meta (segun elegibilidad)",
        "dev": "https://developers.facebook.com/apps",
        "oauth": True,
        "login": "Facebook",
        "fields": [
            {"key": "app_id", "label": "ID de la app de Meta", "secret": False},
            {"key": "app_secret", "label": "Clave secreta de la app", "secret": True},
        ],
        "help": "Registro unico: en developers.facebook.com crea una app tipo 'Empresa', agrega 'Inicio de sesion con "
                "Facebook' y pon esta URI de redireccion valida: {redirect}. Despues pulsa 'Iniciar sesion con "
                "Facebook': con ese inicio de sesion se vinculan a la vez tu Instagram Profesional y tu Pagina "
                "(deben estar enlazados). Las apps en modo desarrollo solo publican en cuentas con rol en la app.",
    },
    "facebook": {
        "name": "Facebook Reels",
        "pays": "Monetizacion de contenido de Facebook",
        "dev": "https://developers.facebook.com/apps",
        "oauth": True,
        "login": "Facebook",
        "fields": [
            {"key": "app_id", "label": "ID de la app de Meta", "secret": False},
            {"key": "app_secret", "label": "Clave secreta de la app", "secret": True},
        ],
        "help": "Usa la misma app de Meta que para Instagram (registro unico) con esta URI de redireccion: {redirect}. "
                "Pulsa 'Iniciar sesion con Facebook' y elige tu Pagina; no hay que copiar ningun token.",
    },
}

META = ("instagram", "facebook")
META_SCOPES = ("pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,"
               "instagram_content_publish,business_management")

lock = threading.RLock()
pending = {}


class PublishError(Exception):
    pass


def redirect_uri(net):
    # Meta (Instagram y Facebook) comparte una sola app y una sola URI de redireccion.
    return f"http://127.0.0.1:{PORT}/oauth/{'facebook' if net in META else net}"


# ---------- almacenamiento ----------
def _load():
    try:
        return json.loads(secreto.unprotect(ACCOUNTS_FILE.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return {}


def _save(data):
    ACCOUNTS_FILE.write_text(secreto.protect(json.dumps(data, indent=1)), encoding="utf-8")


def is_linked(net):
    acc = _load().get(net, {})
    return bool(acc.get("linked"))


def linked_networks():
    return [n for n in NETWORKS if is_linked(n)]


def summary():
    data = _load()
    out = []
    for net, meta in NETWORKS.items():
        acc = data.get(net, {})
        out.append({
            "net": net, "name": meta["name"], "pays": meta["pays"], "oauth": meta["oauth"],
            "login": meta.get("login", meta["name"].split()[0]), "dev_url": meta["dev"],
            "help": meta["help"].format(redirect=redirect_uri(net)),
            "linked": bool(acc.get("linked")), "label": acc.get("label", ""),
            "ready": all(bool(_app_value(net, f["key"])) for f in meta["fields"]),
            "fields": [{"key": f["key"], "label": f["label"], "secret": f["secret"],
                        "saved": bool(_app_value(net, f["key"])),
                        "value": "" if f["secret"] else _app_value(net, f["key"])} for f in meta["fields"]],
        })
    return out


def _app_value(net, key):
    """Datos de la app. Instagram y Facebook comparten los de Meta."""
    data = _load()
    value = data.get(net, {}).get(key, "")
    if not value and net in META:
        value = next((data.get(o, {}).get(key, "") for o in META if o != net), "")
    return value


def unlink(net):
    with lock:
        data = _load()
        data.pop(net, None)
        _save(data)


# ---------- http ----------
def http(method, url, *, headers=None, data=None, json_body=None, form=None, timeout=120):
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise PublishError(f"HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise PublishError(f"Sin conexion con la red social: {exc.reason}")
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return hdrs, parsed


# ---------- vincular ----------
def save_fields(net, values):
    """Guarda los datos de la app (registro unico). Despues se inicia sesion con el boton de la red."""
    meta = NETWORKS[net]
    with lock:
        data = _load()
        acc = data.get(net, {})
        changed = False
        for f in meta["fields"]:
            val = str(values.get(f["key"], "")).strip()
            if val and val != acc.get(f["key"]):
                acc[f["key"]] = val
                changed = True
        if changed:
            for k in ("linked", "label", "refresh_token", "access_token_oauth", "expires_at", "verified"):
                acc.pop(k, None)
        data[net] = acc
        _save(data)
    missing = [f["label"] for f in meta["fields"] if not _app_value(net, f["key"])]
    if missing:
        raise PublishError("Falta: " + ", ".join(missing))


def start_oauth(net):
    acc = _load().get(net, {})
    if net in META:
        app_id, app_secret = _app_value(net, "app_id"), _app_value(net, "app_secret")
        if not app_id or not app_secret:
            raise PublishError("Guarda primero el ID y la clave secreta de tu app de Meta (registro unico).")
        params = {"client_id": app_id, "redirect_uri": redirect_uri(net), "response_type": "code",
                  "scope": META_SCOPES}
        base = "https://www.facebook.com/v21.0/dialog/oauth"
    elif net == "youtube":
        if not acc.get("client_id") or not acc.get("client_secret"):
            raise PublishError("Guarda primero el Client ID y el Client secret.")
        params = {"client_id": acc["client_id"], "redirect_uri": redirect_uri(net), "response_type": "code",
                  "scope": "https://www.googleapis.com/auth/youtube.upload "
                           "https://www.googleapis.com/auth/youtube.readonly",
                  "access_type": "offline", "prompt": "consent"}
        base = "https://accounts.google.com/o/oauth2/v2/auth"
    elif net == "tiktok":
        if not acc.get("client_key") or not acc.get("client_secret"):
            raise PublishError("Guarda primero el Client key y el Client secret.")
        params = {"client_key": acc["client_key"], "redirect_uri": redirect_uri(net), "response_type": "code",
                  "scope": "user.info.basic,video.publish,video.upload"}
        base = "https://www.tiktok.com/v2/auth/authorize/"
    else:
        raise PublishError("Esta red no usa OAuth.")
    state = secrets.token_urlsafe(18)
    with lock:
        for k in [k for k, v in pending.items() if time.time() - v[1] > 900]:
            pending.pop(k)
        pending[state] = (net, time.time())
    params["state"] = state
    return base + "?" + urllib.parse.urlencode(params)


def finish_oauth(net, code, state):
    with lock:
        entry = pending.pop(state, None)
    same = entry and (entry[0] == net or (entry[0] in META and net in META))
    if not same or time.time() - entry[1] > 900:
        raise PublishError("La solicitud de vinculacion expiro o no es valida. Intentalo de nuevo.")
    if net in META:
        return _finish_meta(entry[0], code)
    acc = _load()[net]
    if net == "youtube":
        _, tok = http("POST", "https://oauth2.googleapis.com/token", form={
            "code": code, "client_id": acc["client_id"], "client_secret": acc["client_secret"],
            "redirect_uri": redirect_uri(net), "grant_type": "authorization_code"})
        label_fn = _youtube_label
    else:
        _, tok = http("POST", "https://open.tiktokapis.com/v2/oauth/token/", form={
            "client_key": acc["client_key"], "client_secret": acc["client_secret"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri(net)})
        label_fn = _tiktok_label
    if not isinstance(tok, dict) or not tok.get("access_token"):
        raise PublishError(f"La red no entrego un token: {str(tok)[:200]}")
    if not tok.get("refresh_token"):
        raise PublishError("La red no entrego refresh_token. Quita el acceso de la app en tu cuenta y reintenta.")
    label = label_fn(tok["access_token"])
    with lock:
        data = _load()
        data[net].update({"access_token_oauth": tok["access_token"], "refresh_token": tok["refresh_token"],
                          "expires_at": time.time() + int(tok.get("expires_in", 3000)) - 60,
                          "linked": True, "label": label})
        _save(data)


def _finish_meta(started_by, code):
    """Inicio de sesion con Facebook: deja vinculadas a la vez la Pagina y el Instagram Profesional."""
    app_id, app_secret = _app_value(started_by, "app_id"), _app_value(started_by, "app_secret")
    _, short = http("GET", f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "client_id": app_id, "client_secret": app_secret, "redirect_uri": redirect_uri(started_by), "code": code}))
    if not isinstance(short, dict) or not short.get("access_token"):
        raise PublishError(f"Facebook no entrego un acceso: {str(short)[:200]}")
    _, long_lived = http("GET", f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": short["access_token"]}))
    user_token = (long_lived or {}).get("access_token") if isinstance(long_lived, dict) else None
    user_token = user_token or short["access_token"]
    _, pages = http("GET", f"{GRAPH}/me/accounts?" + urllib.parse.urlencode({
        "fields": "id,name,access_token,instagram_business_account{id,username}", "access_token": user_token}))
    items = pages.get("data", []) if isinstance(pages, dict) else []
    if not items:
        raise PublishError("Tu cuenta no administra ninguna Pagina de Facebook. Crea una o pide acceso y reintenta.")
    page = next((p for p in items if p.get("instagram_business_account")), items[0])
    ig = page.get("instagram_business_account") or {}
    with lock:
        data = _load()
        fb = data.setdefault("facebook", {})
        fb.update({"page_id": page["id"], "access_token": page["access_token"], "linked": True,
                   "label": page.get("name", "Pagina de Facebook")})
        if ig.get("id"):
            insta = data.setdefault("instagram", {})
            insta.update({"ig_user_id": ig["id"], "access_token": page["access_token"], "linked": True,
                          "label": "@" + ig.get("username", "instagram")})
        for net in META:
            data.setdefault(net, {}).setdefault("app_id", app_id)
            data[net].setdefault("app_secret", app_secret)
        _save(data)
    if not ig.get("id"):
        raise PublishError(f"Facebook quedo vinculado ({page.get('name')}), pero esa Pagina no tiene un Instagram "
                           "Profesional enlazado. Enlazalo en Facebook y vuelve a iniciar sesion para Instagram.")


def _youtube_label(token):
    try:
        _, body = http("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                       headers={"Authorization": f"Bearer {token}"})
        return body["items"][0]["snippet"]["title"]
    except Exception:
        return "Canal de YouTube"


def _tiktok_label(token):
    try:
        _, body = http("GET", "https://open.tiktokapis.com/v2/user/info/?fields=display_name",
                       headers={"Authorization": f"Bearer {token}"})
        return body["data"]["user"]["display_name"]
    except Exception:
        return "Cuenta de TikTok"


def _fresh_token(net):
    with lock:
        data = _load()
        acc = data[net]
        if acc.get("expires_at", 0) > time.time() and acc.get("access_token_oauth"):
            return acc["access_token_oauth"]
        if net == "youtube":
            _, tok = http("POST", "https://oauth2.googleapis.com/token", form={
                "client_id": acc["client_id"], "client_secret": acc["client_secret"],
                "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"})
        else:
            _, tok = http("POST", "https://open.tiktokapis.com/v2/oauth/token/", form={
                "client_key": acc["client_key"], "client_secret": acc["client_secret"],
                "grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
        if not isinstance(tok, dict) or not tok.get("access_token"):
            raise PublishError("No pude renovar el acceso. Vuelve a vincular la cuenta.")
        acc["access_token_oauth"] = tok["access_token"]
        acc["expires_at"] = time.time() + int(tok.get("expires_in", 3000)) - 60
        if tok.get("refresh_token"):
            acc["refresh_token"] = tok["refresh_token"]
        _save(data)
        return acc["access_token_oauth"]


# ---------- publicar ----------
def publish(net, video, title, description, hashtags, lang):
    """Sube el video y devuelve su URL (o un texto de confirmacion)."""
    if not is_linked(net):
        raise PublishError("La cuenta no esta vinculada.")
    video = Path(video)
    tags = " ".join(hashtags)
    caption = f"{title}\n\n{description}\n\n{tags}".strip()
    return {"youtube": _pub_youtube, "tiktok": _pub_tiktok,
            "instagram": _pub_instagram, "facebook": _pub_facebook}[net](video, title, description, tags, caption, lang)


def _pub_youtube(video, title, description, tags, caption, lang):
    token = _fresh_token("youtube")
    meta = {"snippet": {"title": title[:100], "description": f"{description}\n\n{tags} #Shorts"[:4900],
                        "tags": [t.lstrip("#") for t in tags.split()], "categoryId": "22",
                        "defaultLanguage": lang, "defaultAudioLanguage": lang},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    size = video.stat().st_size
    hdrs, _ = http("POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                   headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                            "X-Upload-Content-Length": str(size)}, json_body=meta)
    location = hdrs.get("location")
    if not location:
        raise PublishError("YouTube no entrego la URL de subida.")
    _, body = http("PUT", location, headers={"Content-Type": "video/mp4"}, data=video.read_bytes(), timeout=1200)
    if not isinstance(body, dict) or "id" not in body:
        raise PublishError(f"YouTube respondio algo inesperado: {str(body)[:200]}")
    return f"https://youtube.com/shorts/{body['id']}"


def _pub_tiktok(video, title, description, tags, caption, lang):
    token = _fresh_token("tiktok")
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    _, info = http("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", headers=auth)
    options = (info.get("data") or {}).get("privacy_level_options") or ["SELF_ONLY"]
    privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
    size = video.stat().st_size
    _, init = http("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/", headers=auth, json_body={
        "post_info": {"title": caption[:2200], "privacy_level": privacy},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}})
    data = init.get("data") or {}
    if not data.get("upload_url"):
        raise PublishError(f"TikTok rechazo la subida: {str(init)[:250]}")
    http("PUT", data["upload_url"], headers={"Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size - 1}/{size}"},
         data=video.read_bytes(), timeout=1200)
    note = "" if privacy == "PUBLIC_TO_EVERYONE" else f" (privacidad: {privacy})"
    return f"Enviado a TikTok{note}, id {data.get('publish_id', '?')}"


def _meta_upload(url, token, video):
    http("POST", url, headers={"Authorization": f"OAuth {token}", "offset": "0",
                               "file_size": str(video.stat().st_size)}, data=video.read_bytes(), timeout=1200)


def _pub_instagram(video, title, description, tags, caption, lang):
    acc = _load()["instagram"]
    ig, token = acc["ig_user_id"], acc["access_token"]
    _, created = http("POST", f"{GRAPH}/{ig}/media", form={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "access_token": token})
    if not isinstance(created, dict) or "id" not in created or "uri" not in created:
        raise PublishError(f"Instagram rechazo el contenedor: {str(created)[:250]}")
    _meta_upload(created["uri"], token, video)
    for _ in range(60):
        _, st = http("GET", f"{GRAPH}/{created['id']}?" + urllib.parse.urlencode(
            {"fields": "status_code,status", "access_token": token}))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise PublishError(f"Instagram no pudo procesar el video: {st.get('status', '')}")
        time.sleep(5)
    else:
        raise PublishError("Instagram tardo demasiado en procesar el video.")
    _, pub = http("POST", f"{GRAPH}/{ig}/media_publish", form={"creation_id": created["id"], "access_token": token})
    try:
        _, link = http("GET", f"{GRAPH}/{pub['id']}?" + urllib.parse.urlencode(
            {"fields": "permalink", "access_token": token}))
        return link.get("permalink") or f"Instagram media {pub['id']}"
    except Exception:
        return f"Instagram media {pub.get('id', '?')}"


def _pub_facebook(video, title, description, tags, caption, lang):
    acc = _load()["facebook"]
    page, token = acc["page_id"], acc["access_token"]
    _, start = http("POST", f"{GRAPH}/{page}/video_reels", form={"upload_phase": "start", "access_token": token})
    if not isinstance(start, dict) or "upload_url" not in start:
        raise PublishError(f"Facebook rechazo la subida: {str(start)[:250]}")
    _meta_upload(start["upload_url"], token, video)
    _, done = http("POST", f"{GRAPH}/{page}/video_reels", form={
        "upload_phase": "finish", "video_id": start["video_id"], "video_state": "PUBLISHED",
        "description": caption[:2200], "title": title[:100], "access_token": token})
    if isinstance(done, dict) and done.get("success") is False:
        raise PublishError(f"Facebook no pudo publicar: {str(done)[:250]}")
    return f"https://www.facebook.com/reel/{start['video_id']}"
