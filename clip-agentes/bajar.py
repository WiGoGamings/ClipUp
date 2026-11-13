import subprocess
import os

enlace = input("Pega el enlace de TU video o directo: ")

os.makedirs("videos", exist_ok=True)

print("Bajando...")
subprocess.run([
    "yt-dlp",
    "-f", "bv*[height<=1080]+ba/b[height<=1080]",
    "--merge-output-format", "mp4",
    "-o", "videos/%(title)s.%(ext)s",
    enlace
])

print("Listo. Revisa la carpeta videos.")
