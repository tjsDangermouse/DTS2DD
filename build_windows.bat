@echo off
REM Build script for Windows: app folder + installer.
REM
REM The recommended way to build the installer is the GitHub Actions workflow
REM (.github\workflows\windows-installer.yml), which also builds LibreMPEG and
REM runs all tests on Windows. This script does the same packaging locally if
REM you provide the tools yourself:
REM
REM   vendor\ffmpeg\ffmpeg.exe, ffprobe.exe, LICENSE     (gyan.dev essentials build)
REM   vendor\librempeg\ffmpeg.exe, ffprobe.exe, COPYING.AGPLv3, LICENSE.md
REM
REM and Inno Setup 6.3+ (https://jrsoftware.org/isinfo.php) for the installer.

setlocal
echo DTS to AC-3 Converter - Windows Build
echo ====================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Please install Python 3.11+
    echo Download from: https://www.python.org/
    echo Make sure to check "Add Python to PATH"
    exit /b 1
)

for /f %%v in ('python -c "from version import __version__; print(__version__)"') do set APPVER=%%v
echo Version: %APPVER%

echo.
echo 1. Installing build dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pillow

echo.
echo 2. Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo 3. Creating application icon...
python create_icon.py

echo.
echo 4. Building application folder (dist\DTS2DD)...
python -m PyInstaller --noconfirm --clean converter.spec
if not exist "dist\DTS2DD\DTS to AC-3 Converter.exe" (
    echo Build failed! Check error messages above.
    exit /b 1
)

echo.
echo 5. Adding FFmpeg, LibreMPEG and licences...
mkdir "dist\DTS2DD\licenses"
copy /y "installer\THIRD_PARTY_NOTICES.txt" "dist\DTS2DD\licenses\" >nul
if exist "vendor\ffmpeg\ffmpeg.exe" (
    mkdir "dist\DTS2DD\ffmpeg"
    copy /y "vendor\ffmpeg\ffmpeg.exe" "dist\DTS2DD\ffmpeg\" >nul
    copy /y "vendor\ffmpeg\ffprobe.exe" "dist\DTS2DD\ffmpeg\" >nul
    if exist "vendor\ffmpeg\LICENSE" copy /y "vendor\ffmpeg\LICENSE" "dist\DTS2DD\licenses\FFmpeg-LICENSE.txt" >nul
) else (
    echo    WARNING: vendor\ffmpeg\ffmpeg.exe not found - the app will download FFmpeg on first run.
)
if exist "vendor\librempeg\ffmpeg.exe" (
    mkdir "dist\DTS2DD\librempeg"
    copy /y "vendor\librempeg\ffmpeg.exe" "dist\DTS2DD\librempeg\" >nul
    copy /y "vendor\librempeg\ffprobe.exe" "dist\DTS2DD\librempeg\" >nul
    if exist "vendor\librempeg\COPYING.AGPLv3" copy /y "vendor\librempeg\COPYING.AGPLv3" "dist\DTS2DD\licenses\LibreMPEG-COPYING.AGPLv3.txt" >nul
    if exist "vendor\librempeg\LICENSE.md" copy /y "vendor\librempeg\LICENSE.md" "dist\DTS2DD\licenses\LibreMPEG-LICENSE.md" >nul
) else (
    echo    WARNING: vendor\librempeg\ffmpeg.exe not found - TrueHD users must select LibreMPEG in the app.
)

echo.
echo 6. Checking the packaged app...
start "" /wait "dist\DTS2DD\DTS to AC-3 Converter.exe" --self-test "%CD%\selftest.json"
if errorlevel 1 (
    echo    Self-test did not pass - see selftest.json. TrueHD will not be available.
) else (
    echo    Self-test passed: bundled FFmpeg and TrueHD encoder are working.
)

echo.
set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if exist "%ISCC%" (
    echo 7. Compiling installer...
    "%ISCC%" /DAppVersion=%APPVER% installer\DTS2DD.iss
    if errorlevel 1 (
        echo Installer build failed!
        exit /b 1
    )
    echo.
    echo Build complete!
    echo   Installer: dist\installer\DTS2DD-Setup-%APPVER%.exe
) else (
    echo 7. Inno Setup not found - skipping installer.
    echo    Install Inno Setup 6.3+ to produce dist\installer\DTS2DD-Setup-%APPVER%.exe
    echo.
    echo Build complete!
    echo   App folder: dist\DTS2DD\  (run "DTS to AC-3 Converter.exe")
)
endlocal
