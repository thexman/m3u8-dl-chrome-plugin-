@echo off
rem Chrome launches this wrapper; stdout is the native messaging channel,
rem so nothing else may be printed here.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0native_host.py" %*
) else (
  python "%~dp0native_host.py" %*
)
