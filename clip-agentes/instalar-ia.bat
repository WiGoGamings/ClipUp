@echo off
chcp 65001 >nul
title Clip Agentes - instalando la IA local
echo.
echo  Instalando la IA local de Clip Agentes (Ollama + modelo qwen2.5:7b, unos 5 GB).
echo  Necesita internet. Puede tardar un rato; no cierres esta ventana.
echo.

set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
where ollama >nul 2>nul && set "OLLAMA=ollama"

if not exist "%OLLAMA%" if not "%OLLAMA%"=="ollama" (
  echo  Instalando Ollama...
  winget install -e --id Ollama.Ollama --silent --accept-source-agreements --accept-package-agreements
)

if not exist "%OLLAMA%" if not "%OLLAMA%"=="ollama" (
  echo.
  echo  No pude instalar Ollama solo. Descargalo e instalalo desde https://ollama.com/download
  echo  y luego ejecuta este archivo otra vez.
  start https://ollama.com/download
  pause
  exit /b 1
)

echo  Encendiendo Ollama...
start "" /min "%OLLAMA%" serve
timeout /t 6 /nobreak >nul

echo  Descargando el modelo qwen2.5:7b...
"%OLLAMA%" pull qwen2.5:7b
if errorlevel 1 (
  echo.
  echo  No se pudo descargar el modelo. Revisa tu internet y ejecuta este archivo otra vez.
  pause
  exit /b 1
)

echo.
echo  Listo. La IA local ya esta instalada.
timeout /t 4 >nul
