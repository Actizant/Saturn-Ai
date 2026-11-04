#!/usr/bin/env bash
# ==============================================================================
# Saturn - Universal macOS & Linux Installer
# ==============================================================================
set -e

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "==============================================================="
echo "               SATURN - AI ASSISTANT SETUP                 "
echo "                macOS & Linux Universal Installer              "
echo "==============================================================="
echo -e "${NC}"

OS_TYPE="$(uname -s)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1. Detect Python 3.10+
echo -e "${YELLOW}[1/5] Detecting Python environment...${NC}"
PYTHON_BIN=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_BIN="$cmd"
            echo -e "${GREEN}✓ Found compatible Python: $cmd (v$VER)${NC}"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}Error: Python 3.10 or higher is required.${NC}"
    if [ "$OS_TYPE" = "Darwin" ]; then
        echo -e "Install using Homebrew: ${CYAN}brew install python@3.12${NC}"
    else
        echo -e "Install on Ubuntu/Debian: ${CYAN}sudo apt install python3 python3-venv python3-pip${NC}"
        echo -e "Install on Fedora: ${CYAN}sudo dnf install python3 python3-pip${NC}"
        echo -e "Install on Arch: ${CYAN}sudo pacman -S python python-pip${NC}"
    fi
    exit 1
fi

# 2. Setup Virtual Environment
INSTALL_DIR="$HOME/.saturn"
echo -e "${YELLOW}[2/5] Setting up virtual environment at $INSTALL_DIR...${NC}"
mkdir -p "$INSTALL_DIR"

# Copy or sync files if running from clone
if [ -f "$SCRIPT_DIR/main.py" ]; then
    echo "Syncing repository files into $INSTALL_DIR..."
    rsync -av --exclude='.git' --exclude='.venv' --exclude='dist' --exclude='build' --exclude='node_modules' "$SCRIPT_DIR/" "$INSTALL_DIR/" >/dev/null 2>&1 || cp -R "$SCRIPT_DIR/"* "$INSTALL_DIR/" 2>/dev/null || true
fi

cd "$INSTALL_DIR"
if [ ! -d "venv" ]; then
    "$PYTHON_BIN" -m venv venv
fi

VENV_PYTHON="$INSTALL_DIR/venv/bin/python"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"

# 3. Install Dependencies
echo -e "${YELLOW}[3/5] Installing core dependencies...${NC}"
"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install -r requirements.txt

# 4. OS-Specific Desktop Integrations & Launchers
echo -e "${YELLOW}[4/5] Configuring system shortcuts & launchers...${NC}"

if [ "$OS_TYPE" = "Darwin" ]; then
    # --- macOS Setup ---
    APP_BUNDLE="$HOME/Applications/Saturn.app"
    mkdir -p "$HOME/Applications"
    mkdir -p "$APP_BUNDLE/Contents/MacOS"
    mkdir -p "$APP_BUNDLE/Contents/Resources"

    # Info.plist
    cat <<EOF > "$APP_BUNDLE/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDisplayName</key>
    <string>Saturn</string>
    <key>CFBundleExecutable</key>
    <string>Saturn</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.actizant.saturn</string>
    <key>CFBundleName</key>
    <string>Saturn</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

    # Launcher wrapper
    cat <<EOF > "$APP_BUNDLE/Contents/MacOS/Saturn"
#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:\$PATH"
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/main.py" "\$@"
EOF
    chmod +x "$APP_BUNDLE/Contents/MacOS/Saturn"

    # Copy Icon if available
    if [ -f "$INSTALL_DIR/assets/Saturn_Logo.ico" ]; then
        cp "$INSTALL_DIR/assets/Saturn_Logo.ico" "$APP_BUNDLE/Contents/Resources/AppIcon.icns" 2>/dev/null || true
    fi

    echo -e "${GREEN}✓ Created macOS application bundle: $APP_BUNDLE${NC}"

else
    # --- Linux Setup ---
    APPS_DIR="$HOME/.local/share/applications"
    ICONS_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$APPS_DIR"
    mkdir -p "$ICONS_DIR"

    # Copy Icon
    if [ -f "$INSTALL_DIR/assets/Saturn_Logo.ico" ]; then
        cp "$INSTALL_DIR/assets/Saturn_Logo.ico" "$ICONS_DIR/saturn.ico" 2>/dev/null || true
    fi

    # Create .desktop file
    cat <<EOF > "$APPS_DIR/saturn.desktop"
[Desktop Entry]
Type=Application
Name=Saturn
Comment=Autonomous AI Desktop Assistant
Exec=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py
Icon=$ICONS_DIR/saturn.ico
Terminal=false
Categories=Utility;AudioVideo;Development;
StartupNotify=true
EOF
    chmod +x "$APPS_DIR/saturn.desktop"
    echo -e "${GREEN}✓ Created Linux desktop entry: $APPS_DIR/saturn.desktop${NC}"
fi

# 5. CLI Global Command
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat <<EOF > "$BIN_DIR/saturn"
#!/bin/bash
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/saturn"

echo -e "${YELLOW}[5/5] Finalizing installation...${NC}"
echo ""
echo -e "${GREEN}===============================================================${NC}"
echo -e "${GREEN}       Saturn has been successfully installed!             ${NC}"
echo -e "${GREEN}===============================================================${NC}"
echo ""
echo -e "You can launch Saturn by:"
if [ "$OS_TYPE" = "Darwin" ]; then
    echo -e "  1. Opening ${CYAN}Saturn${NC} in your Applications folder."
    echo -e "  2. Running ${CYAN}saturn${NC} in your terminal (ensure ~/.local/bin is in PATH)."
else
    echo -e "  1. Launching ${CYAN}Saturn${NC} from your Application Menu / Dash."
    echo -e "  2. Running ${CYAN}saturn${NC} in your terminal (ensure ~/.local/bin is in PATH)."
fi
echo -e "To uninstall anytime, run: ${CYAN}bash $INSTALL_DIR/installer/uninstall.sh${NC}"
echo ""
