@echo off
rem Registers the native messaging host for Google Chrome on Windows.
rem Usage: install.bat [EXTENSION_ID]
setlocal enabledelayedexpansion

set "HOST_NAME=com.m3u8.downloader"

echo ============================================
echo  M3U8 Downloader - Native Host Installer
echo ============================================
echo.

set "EXT_DIR=%~dp0"
if "%EXT_DIR:~-1%"=="\" set "EXT_DIR=%EXT_DIR:~0,-1%"

rem Prefer a prebuilt native_host.exe (from the GitHub release) over Python.
if exist "%EXT_DIR%\native_host.exe" (
  set "HOST_PATH=%EXT_DIR%\native_host.exe"
) else (
  set "HOST_PATH=%EXT_DIR%\native_host.bat"
  where py >nul 2>nul || where python >nul 2>nul || (
    echo ERROR: Python was not found. Install Python 3.8+ from python.org
    echo        or use the prebuilt native_host.exe from the release page.
    pause
    exit /b 1
  )
)

echo Native host: %HOST_PATH%
echo.

set "EXT_ID=%~1"
rem Release packages pin the extension ID (manifest "key") and ship it here.
if "%EXT_ID%"=="" if exist "%EXT_DIR%\extension_id.txt" (
  set /p EXT_ID=<"%EXT_DIR%\extension_id.txt"
  echo Extension ID: !EXT_ID! ^(from extension_id.txt^)
)
if "%EXT_ID%"=="" (
  echo Open Chrome, go to chrome://extensions, enable Developer mode,
  echo find "M3U8 Downloader" and copy its ID.
  echo.
  set /p EXT_ID="Paste the extension ID here: "
)

if "%EXT_ID%"=="" (
  echo ERROR: Extension ID cannot be empty.
  pause
  exit /b 1
)

set "MANIFEST=%EXT_DIR%\%HOST_NAME%.json"
set "HOST_PATH_JSON=%HOST_PATH:\=\\%"

(
echo {
echo   "name": "%HOST_NAME%",
echo   "description": "M3U8 Downloader native host",
echo   "path": "%HOST_PATH_JSON%",
echo   "type": "stdio",
echo   "allowed_origins": [
echo     "chrome-extension://%EXT_ID%/"
echo   ]
echo }
) > "%MANIFEST%"

echo Written: %MANIFEST%

reg add "HKCU\Software\Google\Chrome\NativeMessagingHosts\%HOST_NAME%" /ve /t REG_SZ /d "%MANIFEST%" /f >nul
if not %errorlevel%==0 (
  echo.
  echo ERROR: Failed to write the registry key.
  pause
  exit /b 1
)

echo Registered: HKCU\Software\Google\Chrome\NativeMessagingHosts\%HOST_NAME%
echo.

where ffmpeg >nul 2>nul || (
  echo WARNING: ffmpeg was not found on PATH. Install it ^(e.g. "winget install Gyan.FFmpeg"^)
  echo          or put the full path to ffmpeg.exe in the extension settings.
  echo.
)

echo SUCCESS! Reload the extension in chrome://extensions and you're ready.
echo.
pause
