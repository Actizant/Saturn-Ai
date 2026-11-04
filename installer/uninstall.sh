#!/usr/bin/env bash
# ==============================================================================
# Saturn - macOS & Linux Uninstaller
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${RED}"
echo "==============================================================="
echo "               SATURN - UNINSTALL WIZARD                   "
echo "==============================================================="
echo -e "${NC}"

OS_TYPE="$(uname -s)"
INSTALL_DIR="$HOME/.saturn"

# 1. Terminate running instances
echo -e "${YELLOW}[1/4] Stopping active Saturn processes...${NC}"
pkill -f "main.py" 2>/dev/null || true
pkill -f "Saturn" 2>/dev/null || true

# 2. Remove Shortcuts / Application Bundles
echo -e "${YELLOW}[2/4] Removing system launchers & shortcuts...${NC}"
if [ "$OS_TYPE" = "Darwin" ]; then
    rm -rf "$HOME/Applications/Saturn.app" 2>/dev/null || true
    echo "Removed macOS Application bundle."
else
    rm -f "$HOME/.local/share/applications/saturn.desktop" 2>/dev/null || true
    rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/saturn.ico" 2>/dev/null || true
    echo "Removed Linux desktop entry."
fi

# Remove CLI alias
rm -f "$HOME/.local/bin/saturn" 2>/dev/null || true

# 3. Prompt for user data removal
echo -e "${YELLOW}[3/4] User configuration options...${NC}"
read -p "Do you also want to remove personal data, history, and models (~/SaturnAI)? [y/N]: " REMOVE_DATA
if [[ "$REMOVE_DATA" =~ ^[Yy]$ ]]; then
    rm -rf "$HOME/SaturnAI" 2>/dev/null || true
    rm -rf "$HOME/.config/SaturnAI" 2>/dev/null || true
    echo "Removed personal data and cache."
else
    echo "Preserved user data in ~/SaturnAI."
fi

# 4. Remove installation files
echo -e "${YELLOW}[4/4] Removing application directory...${NC}"
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
fi

echo ""
echo -e "${GREEN}===============================================================${NC}"
echo -e "${GREEN}       Saturn has been cleanly uninstalled!               ${NC}"
echo -e "${GREEN}===============================================================${NC}"
echo ""
