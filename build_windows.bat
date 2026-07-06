@echo off
REM Build script for Windows

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

echo Checking Python architecture...
python -c "import struct; arch = struct.calcsize('P') * 8; print('Python: ' + str(arch) + '-bit'); import sys; print('Version: ' + sys.version)" || (
    echo Error: Could not check Python architecture
    exit /b 1
)

echo.
echo 1. Installing build dependencies...
pip install --upgrade pip setuptools
pip install pyinstaller pillow py7zr

echo.
echo 2. Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "DTS to AC-3 Converter.spec.bak" del "DTS to AC-3 Converter.spec.bak"

echo.
echo 3. Creating application icon...
python create_icon.py

echo.
echo 4. Building Windows executable (64-bit)...
python -m PyInstaller --clean converter.spec

echo.
if exist "dist\DTS to AC-3 Converter.exe" (
    echo 4. Build complete!
    echo.
    echo Output: dist\DTS to AC-3 Converter.exe
    echo.
    echo To run:
    echo   dist\DTS to AC-3 Converter.exe
    echo.
    echo To create shortcut:
    echo   1. Right-click dist\DTS to AC-3 Converter.exe
    echo   2. Send to ^> Desktop (create shortcut)
    echo   3. Rename shortcut as desired
    echo.
) else (
    echo Build failed! Check error messages above.
    exit /b 1
)
