#!/bin/bash
# Build script for macOS

set -e

echo "DTS to AC-3 Converter - macOS Build"
echo "===================================="
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Please install Python 3.11+"
    exit 1
fi

echo "1. Installing build dependencies..."
pip install --upgrade pyinstaller pillow py7zr

echo ""
echo "2. Creating application icon..."
python3 create_icon.py

echo ""
echo "3. Building macOS app bundle..."
pyinstaller --clean converter.spec

echo ""
echo "4. Build complete!"
echo ""
echo "Output: dist/DTS to AC-3 Converter.app"
echo ""
echo "To install to Applications folder:"
echo "  cp -r dist/DTS\ to\ AC-3\ Converter.app /Applications/"
echo ""
echo "To run:"
echo "  open dist/DTS\ to\ AC-3\ Converter.app"
echo ""
