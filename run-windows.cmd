@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === 10-K Dashboard launcher ===
echo.

where docker >nul 2>nul
if errorlevel 1 (
  echo [X] Docker was not found.
  echo     Install Docker Desktop first: https://www.docker.com/products/docker-desktop/
  echo     See WINDOWS.md for a step-by-step guide.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo [*] Creating .env from .env.example ...
  copy /y ".env.example" ".env" >nul
  echo     Open .env and set EDGAR_IDENTITY to your name and email.
  echo.
)

docker info >nul 2>nul
if errorlevel 1 (
  echo [X] Docker Desktop is not running.
  echo     Start Docker Desktop, wait for the whale icon to settle, then run this again.
  echo.
  pause
  exit /b 1
)

echo [*] Building and starting the dashboard ^(the first run can take a few minutes^) ...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo [X] Failed to start. See the messages above.
  echo.
  pause
  exit /b 1
)

set "HTTP_PORT=8000"
for /f "tokens=1,2 delims==" %%A in (.env) do (
  if /i "%%A"=="HTTP_PORT" set "HTTP_PORT=%%B"
)

echo.
echo [OK] Dashboard is starting at http://localhost:!HTTP_PORT!
echo      You can close this window; the container keeps running.
start "" "http://localhost:!HTTP_PORT!"
echo.
pause
