@echo off
setlocal enabledelayedexpansion
REM install.bat — install RabbitViewer CLI tools on Windows.
REM
REM Usage:
REM   install.bat             — install / reinstall
REM   install.bat --update    — pull latest changes, then reinstall
REM   install.bat --clean     — wipe the venv and reinstall from scratch
REM   install.bat --uninstall — remove CLI wrapper from PATH directory
REM
REM What it does:
REM   1. Creates (or reuses) a venv at <repo>\venv using Python 3.10-3.13
REM      (PySide6 does not yet support Python 3.14+).
REM   2. Installs the package in editable mode (source changes take effect immediately).
REM   3. Writes a thin wrapper script into %LOCALAPPDATA%\RabbitViewer\bin\ that
REM      prepends the repo to PYTHONPATH before invoking the venv entry point.
REM   4. Adds the bin directory to the user's PATH if not already present.
REM
REM Requirements: Python 3.10-3.13

set "REPO_DIR=%~dp0"
REM Remove trailing backslash
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"

set "VENV_DIR=%REPO_DIR%\venv"
set "BIN_DIR=%LOCALAPPDATA%\RabbitViewer\bin"
set "MODE=%~1"

REM ── argument validation ─────────────────────────────────────────────────────

if "%MODE%"=="" goto :args_ok
if "%MODE%"=="--clean" goto :args_ok
if "%MODE%"=="--uninstall" goto :args_ok
if "%MODE%"=="--update" goto :args_ok
echo Unknown argument: %MODE%
echo Usage: %~nx0 [--clean^|--uninstall^|--update]
exit /b 1
:args_ok

REM ── uninstall ───────────────────────────────────────────────────────────────

if not "%MODE%"=="--uninstall" goto :skip_uninstall

echo Removing wrappers from %BIN_DIR% ...
if exist "%BIN_DIR%\rabbit.bat" (
    del "%BIN_DIR%\rabbit.bat"
    echo   removed %BIN_DIR%\rabbit.bat
)
if exist "%BIN_DIR%\rabbit.cmd" (
    del "%BIN_DIR%\rabbit.cmd"
    echo   removed %BIN_DIR%\rabbit.cmd
)

REM Remove Start Menu shortcut.
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\RabbitViewer.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo   removed Start Menu shortcut
)

echo Done. The venv at %VENV_DIR% was left intact.
echo Note: the PATH entry was not removed. You can remove it manually from
echo   System Properties ^> Environment Variables ^> User variables ^> Path
exit /b 0

:skip_uninstall

REM ── clean flag ──────────────────────────────────────────────────────────────

set "SAVED_PYTHON="
if not "%MODE%"=="--clean" goto :skip_clean

REM Stop running daemon before wiping.
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo Stopping running daemon (if any) ...
    "%VENV_DIR%\Scripts\python.exe" -c "import logging, sys; logging.basicConfig(level=logging.INFO, format='%%(asctime)s [%%(levelname)s] %%(message)s'); sys.path.insert(0, r'%REPO_DIR%'); from cli.stop import stop_daemon; stop_daemon()" 2>nul
)

REM Remember the Python interpreter.
if exist "%VENV_DIR%\Scripts\python.exe" (
    for /f "delims=" %%P in ('"%VENV_DIR%\Scripts\python.exe" -c "import os,sys; print(os.path.realpath(sys.executable))"') do set "SAVED_PYTHON=%%P"
    echo Remembered venv Python: !SAVED_PYTHON!
)

if exist "%VENV_DIR%" (
    echo Removing existing venv at %VENV_DIR% ...
    rmdir /s /q "%VENV_DIR%"
)
if exist "%REPO_DIR%\rabbitviewer.egg-info" rmdir /s /q "%REPO_DIR%\rabbitviewer.egg-info"

:skip_clean

REM ── optional update ─────────────────────────────────────────────────────────

if not "%MODE%"=="--update" goto :skip_update

REM Stop daemon before replacing code.
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo Stopping running daemon (if any) ...
    "%VENV_DIR%\Scripts\python.exe" -c "import logging, sys; logging.basicConfig(level=logging.INFO, format='%%(asctime)s [%%(levelname)s] %%(message)s'); sys.path.insert(0, r'%REPO_DIR%'); from cli.stop import stop_daemon; stop_daemon()" 2>nul
)

echo Pulling latest changes ...
git -C "%REPO_DIR%" pull --ff-only 2>nul
if errorlevel 1 (
    echo Could not fast-forward (local changes or detached HEAD) — skipping pull.
) else (
    echo Repository up to date.
)

:skip_update

REM ── install ─────────────────────────────────────────────────────────────────

REM 1. Choose the Python interpreter.
set "PYTHON="

REM Try saved Python first (for --clean rebuilds).
if defined SAVED_PYTHON (
    if exist "!SAVED_PYTHON!" (
        call :python_version_ok "!SAVED_PYTHON!" _ok
        if "!_ok!"=="ok" (
            set "PYTHON=!SAVED_PYTHON!"
            echo Using saved venv Python: !PYTHON!
        )
    )
)

REM Search for a compatible Python.
if not defined PYTHON (
    for %%C in (python3.13 python3.12 python3.11 python3.10 python3 python py) do (
        if not defined PYTHON (
            where %%C >nul 2>nul
            if not errorlevel 1 (
                call :python_version_ok "%%C" _ok
                if "!_ok!"=="ok" (
                    set "PYTHON=%%C"
                )
            )
        )
    )
    REM Also try the py launcher with version flags.
    if not defined PYTHON (
        where py >nul 2>nul
        if not errorlevel 1 (
            for %%V in (-3.13 -3.12 -3.11 -3.10) do (
                if not defined PYTHON (
                    py %%V -c "pass" >nul 2>nul
                    if not errorlevel 1 (
                        call :python_version_ok "py %%V" _ok
                        if "!_ok!"=="ok" set "PYTHON=py %%V"
                    )
                )
            )
        )
    )
)

if not defined PYTHON (
    echo No compatible Python found (need 3.10-3.13; PySide6 does not support 3.14+^).
    exit /b 1
)
echo Using Python: %PYTHON%

set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

REM 2. Validate or create the venv.
if exist "%VENV_DIR%" (
    "%VENV_PYTHON%" -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo Existing venv is broken — recreating ...
        rmdir /s /q "%VENV_DIR%"
        if exist "%REPO_DIR%\rabbitviewer.egg-info" rmdir /s /q "%REPO_DIR%\rabbitviewer.egg-info"
        %PYTHON% -m venv "%VENV_DIR%"
    ) else (
        echo Reusing existing venv at %VENV_DIR%
    )
) else (
    echo Creating venv at %VENV_DIR% ...
    %PYTHON% -m venv "%VENV_DIR%"
)

REM 3. Check for exiftool (required).
where exiftool >nul 2>nul
if errorlevel 1 (
    echo [WARNING] exiftool not found in PATH.
    echo   exiftool is required for metadata extraction, RAW support, and rating write-back.
    echo   Download from: https://exiftool.org/
    echo   After downloading, add the folder containing exiftool.exe to your PATH.
    echo.
) else (
    for /f "delims=" %%V in ('exiftool -ver 2^>nul') do echo exiftool found: %%V
)

REM 4. Editable install.
echo Installing RabbitViewer (editable) ...
"%VENV_PIP%" install --upgrade pip
"%VENV_PIP%" install -e "%REPO_DIR%"
if errorlevel 1 (
    echo Package installation failed.
    exit /b 1
)
echo Package installed.

REM 4b. Check optional dependencies.
echo.
echo Checking optional dependencies ...
set "MISSING_OPTIONAL="

REM Video support: python-mpv
"%VENV_PYTHON%" -c "import mpv" >nul 2>nul
if errorlevel 1 (
    echo   [optional] Video playback support not installed.
    echo             Install with: %VENV_PIP% install python-mpv
    echo             Also requires mpv/libmpv — see https://mpv.io/installation/
    set "MISSING_OPTIONAL=1"
)

if not defined MISSING_OPTIONAL (
    echo   All optional dependencies are installed.
)
echo.

REM 4c. AI features (CLIP vector search + auto-rotate + face recognition).
echo.
echo AI Features (optional)
echo   RabbitViewer can use local AI models for:
echo     - CLIP semantic search — find images by text description
echo     - Auto-rotate — detect and fix image orientation
echo     - Face recognition — detect, identify, and filter by person
echo   This installs onnxruntime + numpy (~25 MB) and downloads
echo   ONNX models (~850 MB) to ~/.rabbitviewer/models/ on first use.
echo.
set /p "INSTALL_AI=  Install AI features? [y/N] "
echo.

if /i "%INSTALL_AI%"=="y" (
    echo Installing AI dependencies ...
    "%VENV_PIP%" install -r "%REPO_DIR%\requirements-ai.txt"
    echo   AI dependencies installed.

    echo Downloading AI models (this may take a few minutes) ...
    "%VENV_PYTHON%" -c "import sys; sys.path.insert(0, r'%REPO_DIR%'); from core.model_manager import ensure_model, MODELS; [print(f'  Downloading {n} ...', end=' ', flush=True) or print('OK' if ensure_model(n) else 'FAILED') for n in MODELS]"
    echo   AI models downloaded.
) else (
    echo   Skipping AI features. You can install them later with:
    echo     %VENV_PIP% install -r %REPO_DIR%\requirements-ai.txt
)
echo.

REM 5. Write `rabbit.cmd` wrapper into bin directory.
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

set "RABBIT_EP=%VENV_DIR%\Scripts\rabbit.exe"
set "RABBIT_DST=%BIN_DIR%\rabbit.cmd"

if not exist "%RABBIT_EP%" (
    echo Entry point not found after install: %RABBIT_EP%
    exit /b 1
)

(
    echo @echo off
    echo set "PYTHONPATH=%REPO_DIR%;%%PYTHONPATH%%"
    echo "%RABBIT_EP%" %%*
) > "%RABBIT_DST%"
echo   %RABBIT_DST% -^> %RABBIT_EP%

REM 5e. Create Start Menu shortcut.
REM Uses PowerShell to create a .lnk file — available on all modern Windows.
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\RabbitViewer.lnk"
set "ICON_SRC=%REPO_DIR%\logo\rabbitViewerLogo.ico"

REM Create shortcut regardless of icon availability.
powershell -NoProfile -Command ^
    "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%RABBIT_DST%'; $s.Arguments = 'viewer'; $s.WorkingDirectory = '%USERPROFILE%'; if (Test-Path '%ICON_SRC%') { $s.IconLocation = '%ICON_SRC%' }; $s.Save()" >nul 2>nul
if exist "%SHORTCUT%" (
    echo   Start Menu shortcut created.
) else (
    echo   Could not create Start Menu shortcut (non-fatal^).
)

REM 6. Ensure bin directory is in user PATH.
echo.

REM Check if already in PATH.
echo %PATH% | findstr /i /c:"%BIN_DIR%" >nul
if not errorlevel 1 (
    echo %BIN_DIR% is in PATH.
    goto :path_done
)

REM Check if it's already in the persisted user PATH (but not loaded in this session).
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
if defined USER_PATH (
    echo !USER_PATH! | findstr /i /c:"%BIN_DIR%" >nul
    if not errorlevel 1 (
        echo %BIN_DIR% is in your user PATH but not active in this session.
        echo Open a new terminal for it to take effect.
        goto :path_done
    )
)

REM Add to user PATH via registry.
echo Adding %BIN_DIR% to user PATH ...
if defined USER_PATH (
    setx PATH "%BIN_DIR%;!USER_PATH!" >nul 2>nul
) else (
    setx PATH "%BIN_DIR%" >nul 2>nul
)
if errorlevel 1 (
    echo Could not update PATH automatically.
    echo Add this directory to your PATH manually: %BIN_DIR%
) else (
    echo Done. Open a new terminal for PATH changes to take effect.
)

:path_done

echo.
echo Installation complete.
echo Run: rabbit [directory]
echo CLI: rabbit --help

exit /b 0

REM ── helper functions ────────────────────────────────────────────────────────

:python_version_ok
REM Usage: call :python_version_ok "python_cmd" result_var
REM Sets result_var to "ok" or "no".
set "%~2=no"
for /f "delims=" %%R in ('%~1 -c "import sys; v=sys.version_info; print('ok' if (3,10)<=v<(3,14) else 'no')" 2^>nul') do set "%~2=%%R"
exit /b 0
