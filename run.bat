@echo off
REM Change to the directory where this .bat file is located (the repo root)
cd /d "%~dp0"

REM Ensure Python can import local modules by adding repo root to PYTHONPATH
set PYTHONPATH=%CD%

REM Run the exporter script; forward any arguments passed to the batch file
python run_all.py %*

REM Report exit status and keep the window open so you can see output
if %errorlevel% neq 0 (
  echo.
  echo Script exited with error code %errorlevel%.
) else (
  echo.
  echo Script completed successfully.
)
pause