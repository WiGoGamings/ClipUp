# Clip Agentes

Panel local que convierte videos y directos en clips verticales listos para publicar: los agentes eligen el mejor
momento (audio, imagen, texto, IA y reacciones de la audiencia), lo editan, lo doblan a español e inglés y lo
publican en tus redes. Todo corre en tu PC; nada se envía a servidores propios.

## Requisitos

- Windows, Python 3.10+ y [ffmpeg](https://ffmpeg.org) en el PATH.
- [Ollama](https://ollama.com) con un modelo (por ejemplo `ollama pull qwen2.5:7b`) para la IA. Sin él funciona con análisis básico.

## Uso

```
pip install -r requirements.txt
python server.py        # o doble clic en iniciar.bat
```

Abre http://127.0.0.1:8765. El servidor solo escucha en tu propia PC.

## Qué incluye

- **Cazador viral**: elige el mejor momento con audio, imagen, texto, IA y la reacción de la audiencia (chat, mapa de calor de YouTube y comentarios con minuto).
- **Radar viral**: busca videos y directos de YouTube con muchas vistas o espectadores, avisa en la campana (y por correo, opcional) y baja el video si no hay sesión iniciada.
- **Directos**: graba y saca clips en tiempo real.
- **Cuentas**: YouTube, TikTok, Instagram y Facebook por el inicio de sesión oficial de cada plataforma. Cada una pide crear una app de desarrollador una sola vez.

## Seguridad

Tokens y claves se guardan solo en tu PC (`cuentas.json`, `radar.json`), cifrados con la protección de Windows (DPAPI), y no se suben a git.

## Uso responsable

Publica solo contenido tuyo o con permiso de su autor, y respeta las reglas de cada plataforma.
