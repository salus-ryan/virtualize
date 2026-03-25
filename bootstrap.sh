#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Virtualize — One-line bootstrap
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/jmanhype/virtualize/main/bootstrap.sh | bash
#
# Or after cloning:
#   bash bootstrap.sh
#
# What this does:
#   1. Clones the repo (if not already in it)
#   2. Creates a Python virtual environment
#   3. Installs Virtualize + dependencies
#   4. Runs `virtualize setup` to detect your OS and install QEMU
#   5. Runs `virtualize doctor` to verify everything works
#   6. Shows you how to use it
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/salus-ryan/virtualize.git"
INSTALL_DIR="${VIRTUALIZE_DIR:-$HOME/virtualize}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║          ${GREEN}Virtualize${NC}${BOLD} — Bootstrap Installer            ║${NC}"
echo -e "${BOLD}║  Free, cross-platform VM orchestration for AI       ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 1: Check Python ────────────────────────────────────────────────

info "Checking Python..."
if command -v python3 &>/dev/null; then
    PY=$(command -v python3)
    PY_VERSION=$($PY --version 2>&1 | awk '{print $2}')
    MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
        ok "Python $PY_VERSION"
    else
        fail "Python 3.10+ required (found $PY_VERSION)"
    fi
else
    fail "Python 3 not found. Install it first:
    Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip
    macOS:         brew install python@3.12
    Windows:       winget install Python.Python.3.12"
fi

# ── Step 2: Clone repo ──────────────────────────────────────────────────

if [ -f "pyproject.toml" ] && grep -q "virtualize" pyproject.toml 2>/dev/null; then
    info "Already in Virtualize repo"
    INSTALL_DIR="$(pwd)"
else
    info "Cloning Virtualize → $INSTALL_DIR"
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Directory exists, pulling latest..."
        cd "$INSTALL_DIR"
        git pull --ff-only
    else
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
    ok "Cloned"
fi

cd "$INSTALL_DIR"

# ── Step 3: Create venv ─────────────────────────────────────────────────

info "Creating virtual environment..."
if [ ! -d ".venv" ]; then
    $PY -m venv .venv
fi
ok "Virtual environment ready"

# Activate
source .venv/bin/activate

# ── Step 4: Install ─────────────────────────────────────────────────────

info "Installing Virtualize + dependencies..."
pip install -q -e ".[dev]" 2>&1 | tail -1
ok "Installed"

# ── Step 5: OS detection + QEMU setup ──────────────────────────────────

echo ""
echo -e "${BOLD}── System Detection & Setup ────────────────────────────${NC}"
echo ""

python -m virtualize.cli.main setup

# ── Step 6: Doctor check ────────────────────────────────────────────────

echo ""
echo -e "${BOLD}── System Health Check ─────────────────────────────────${NC}"
echo ""

python -m virtualize.cli.main doctor

# ── Step 7: Verify algebra ──────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Algebraic Axiom Verification ────────────────────────${NC}"
echo ""

python -m virtualize.cli.main algebra verify

# ── Step 8: Run tests ───────────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Running Tests ──────────────────────────────────────${NC}"
echo ""

python -m pytest tests/ -q 2>&1 | tail -3

# ── Done ────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║               ${GREEN}Setup Complete!${NC}${BOLD}                        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Quick Start:${NC}"
echo ""
echo -e "  ${DIM}# Activate the environment${NC}"
echo -e "  ${GREEN}cd $INSTALL_DIR && source .venv/bin/activate${NC}"
echo ""
echo -e "  ${DIM}# Create and start a VM${NC}"
echo -e "  ${GREEN}virtualize create my-vm --cpus 2 --memory 2048${NC}"
echo -e "  ${GREEN}virtualize start <vm_id>${NC}"
echo -e "  ${GREEN}virtualize exec <vm_id> 'uname -a'${NC}"
echo ""
echo -e "  ${DIM}# Run sandboxed code${NC}"
echo -e "  ${GREEN}virtualize sandbox run \"print('hello from sandbox')\" --lang python${NC}"
echo ""
echo -e "  ${DIM}# Web dashboard${NC}"
echo -e "  ${GREEN}python -m uvicorn virtualize.api.server:app --port 8420${NC}"
echo -e "  ${DIM}→ Open http://localhost:8420${NC}"
echo ""
echo -e "  ${DIM}# MCP server (for AI agents)${NC}"
echo -e "  ${GREEN}virtualize mcp serve${NC}"
echo ""
echo -e "  ${DIM}# Validate an execution plan${NC}"
echo -e "  ${GREEN}virtualize algebra validate '[[\"vm_create\",null,{\"name\":\"x\"}],[\"vm_start\",\"x\",{}]]'${NC}"
echo ""
echo -e "  ${DIM}# Compliance report${NC}"
echo -e "  ${GREEN}virtualize compliance report soc2${NC}"
echo ""
echo -e "${BOLD}Docs:${NC} ${BLUE}https://github.com/jmanhype/virtualize${NC}"
echo ""
