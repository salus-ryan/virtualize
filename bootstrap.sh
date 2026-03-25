#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Virtualize — One-line bootstrap
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/salus-ryan/virtualize/main/bootstrap.sh | bash
#
# Or after cloning:
#   bash bootstrap.sh
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/salus-ryan/virtualize.git"
INSTALL_DIR="${VIRTUALIZE_DIR:-$HOME/virtualize}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()    { echo -e "${BLUE}▸${NC} $*"; }
ok()      { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
fail()    { echo -e "${RED}✗${NC} $*"; exit 1; }
section() { echo ""; echo -e "${BOLD}── $* ──────────────────────────────────────────────${NC}"; echo ""; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║            ${GREEN}Virtualize${NC}${BOLD} — Bootstrap Installer              ║${NC}"
echo -e "${BOLD}║    Free, cross-platform VM orchestration for AI         ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
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
    info "Cloning Virtualize into $INSTALL_DIR"
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

source .venv/bin/activate

# ── Step 4: Install ─────────────────────────────────────────────────────

info "Installing Virtualize + dependencies..."
pip install -q -e ".[dev]" 2>&1 | tail -1
ok "Installed"

# ── Step 5: OS detection + QEMU setup ──────────────────────────────────

section "Step 1/4: System Detection"
python -m virtualize.cli.main setup

# ── Step 6: Doctor check ────────────────────────────────────────────────

section "Step 2/4: Health Check"
python -m virtualize.cli.main doctor

# ── Step 7: Verify algebra ──────────────────────────────────────────────

section "Step 3/4: Verify Algebra"
python -m virtualize.cli.main algebra verify

# ── Step 8: Run tests ───────────────────────────────────────────────────

section "Step 4/4: Run Tests"
python -m pytest tests/ -q 2>&1 | tail -3

# ═══════════════════════════════════════════════════════════════════════════
# ELI5 Algebra Demo — show it working, not just theory
# ═══════════════════════════════════════════════════════════════════════════

section "How Virtualize Works (live demo)"

echo -e "${BOLD}Virtualize uses math to keep your VMs safe.${NC}"
echo ""
echo -e "Every VM operation (create, start, exec, stop, destroy) has ${BOLD}rules${NC}."
echo -e "The rules say: ${CYAN}\"you can only do X when the VM is in state Y.\"${NC}"
echo -e "Virtualize checks these rules ${BOLD}before${NC} doing anything."
echo ""
echo -e "Let's see it in action:"
echo ""

# Demo 1: Valid plan
echo -e "${BOLD}  Demo 1: A valid plan${NC}"
echo -e "  ${DIM}(create a VM, start it, run a command, stop it, destroy it)${NC}"
echo ""
python -m virtualize.cli.main algebra validate \
  '[["vm_create",null,{"name":"demo-vm"}],["vm_start","demo-vm",{}],["vm_exec","demo-vm",{"command":"echo hello"}],["vm_stop","demo-vm",{}],["vm_destroy","demo-vm",{}]]'
echo ""

# Demo 2: Invalid plan
echo -e "${BOLD}  Demo 2: An invalid plan (caught by the algebra)${NC}"
echo -e "  ${DIM}(trying to run a command on a VM that doesn't exist)${NC}"
echo ""
python -m virtualize.cli.main algebra validate \
  '[["vm_exec","ghost-vm",{"command":"echo hello"}]]' || true
echo ""

# Demo 3: Optimized plan
echo -e "${BOLD}  Demo 3: Algebraic optimization${NC}"
echo -e "  ${DIM}(removing redundant steps automatically)${NC}"
echo ""
python -m virtualize.cli.main algebra rewrite \
  '[["identity",null,{}],["vm_create","x",{"name":"x"}],["identity",null,{}],["vm_status","x",{}],["vm_status","x",{}],["vm_start","x",{}]]'
echo ""

echo -e "${BOLD}That's it.${NC} The algebra guarantees that ${GREEN}valid plans run${NC},"
echo -e "and ${RED}invalid plans are caught before they touch anything${NC}."
echo ""

# Demo 4: Actually create a VM to get a real ID
section "Live VM Demo"

echo -e "Creating a real VM to show the full workflow..."
echo ""

VM_OUTPUT=$(python -m virtualize.cli.main create demo-vm --cpus 2 --memory 2048 2>&1)
echo "$VM_OUTPUT"

# Extract the VM ID from output
VM_ID=$(echo "$VM_OUTPUT" | grep -oP '[a-f0-9]{12}' | head -1 || true)

if [ -n "$VM_ID" ]; then
    echo ""
    echo -e "${GREEN}Your VM ID is: ${BOLD}${VM_ID}${NC}"
    echo ""
    echo -e "Now you can run these commands (copy-paste one at a time):"
    echo ""
    echo -e "    ${CYAN}virtualize start ${VM_ID}${NC}"
    echo -e "    ${CYAN}virtualize exec ${VM_ID} 'uname -a'${NC}"
    echo -e "    ${CYAN}virtualize stop ${VM_ID}${NC}"
    echo -e "    ${CYAN}virtualize destroy ${VM_ID}${NC}"
else
    echo ""
    echo -e "${YELLOW}Could not extract VM ID. Run 'virtualize list' to see your VMs.${NC}"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Final summary — safe, non-executable reference card
# ═══════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║                 ${GREEN}Setup Complete!${NC}${BOLD}                          ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

cat << 'CHEATSHEET'
┌──────────────────────────────────────────────────────────────┐
│                    COMMAND CHEAT SHEET                        │
│                                                              │
│  Copy-paste these ONE AT A TIME into your terminal.          │
│                                                              │
│  FIRST, activate the environment:                            │
│                                                              │
│      source .venv/bin/activate                               │
│                                                              │
│  VM LIFECYCLE:                                               │
│      virtualize create my-vm --cpus 2 --memory 2048         │
│      virtualize list                                         │
│      virtualize start VM_ID_FROM_LIST                        │
│      virtualize exec VM_ID_FROM_LIST 'uname -a'             │
│      virtualize stop VM_ID_FROM_LIST                         │
│      virtualize destroy VM_ID_FROM_LIST                      │
│                                                              │
│  SANDBOXED CODE:                                             │
│      virtualize sandbox run "print('hi')" --lang python      │
│                                                              │
│  NATURAL LANGUAGE (requires: pip install -e '.[agent]'):     │
│      virtualize ask "create a vm and run uname"              │
│      virtualize ask "check hipaa compliance"                 │
│                                                              │
│  ALGEBRA:                                                    │
│      virtualize algebra verify                               │
│      virtualize algebra state                                │
│                                                              │
│  COMPLIANCE:                                                 │
│      virtualize compliance report soc2                       │
│                                                              │
│  WEB DASHBOARD (open http://localhost:8420 in browser):      │
│      uvicorn virtualize.api.server:app --port 8420           │
│                                                              │
│  MCP SERVER (for AI agents — blocks until Ctrl+C):           │
│      virtualize mcp serve                                    │
│                                                              │
│  Replace VM_ID_FROM_LIST with the actual ID shown by         │
│  'virtualize list' or 'virtualize create'.                   │
│                                                              │
│  Docs: https://github.com/salus-ryan/virtualize             │
│  LLM context: see AGENTS.md in the repo root                │
└──────────────────────────────────────────────────────────────┘
CHEATSHEET

echo ""
