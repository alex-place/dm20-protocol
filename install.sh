#!/usr/bin/env bash
# DM20 Protocol — Interactive Installer
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh)
# Or:    bash install.sh           (from the repo root)
# Or:    bash install.sh --upgrade [play_dir]   (update existing install — auto-detects play dir)
# Or:    bash install.sh --voice   [play_dir]   (add TTS voice narration support)
#
# Add --plugin to a fresh user-mode install or to --upgrade to install the /dm
# commands, persona and hooks as the Claude Code plugin "dm" (see plugin/)
# instead of copying them into the play directory's .claude/.

set -euo pipefail

REPO_URL="https://github.com/Polloinfilzato/dm20-protocol.git"
RAW_BASE="https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main"
# Fetch version dynamically from pyproject.toml (single source of truth)
_remote_ver=$(curl -fsSL "${RAW_BASE}/pyproject.toml" 2>/dev/null | grep -E '^version' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
VERSION="${_remote_ver:-0.3.0}"  # Fallback if fetch fails
UPGRADE_MODE=false       # true when running with --upgrade flag
VOICE_MODE=false         # true when running with --voice flag
PLUGIN_MODE=false        # true when running with --plugin flag
PLUGIN_NAME="dm"                     # plugin name (commands become /dm:*)
PLUGIN_MARKETPLACE="dm20-protocol"   # marketplace name in plugin/.claude-plugin/marketplace.json
PLUGIN_SRC_DIR="${HOME}/.dm20-protocol/src"  # local clone used as the marketplace source

# ─── Global State ─────────────────────────────────────────────────────────────

INSTALL_MODE=""          # "user" or "developer"
INSIDE_CLONE=false       # true when running from inside an existing clone
INSTALL_DIR=""           # developer mode: repo directory
PLAY_DIR=""              # user mode: play directory
DATA_DIR=""              # both modes: campaign data directory
CLONE_NEEDED=false       # developer mode: whether to git clone
MCP_CLIENT=""            # "desktop", "code", or "both"
CODE_SCOPE="global"      # "global" or "project"
INSTALL_RAG=false        # whether to install RAG extras
INSTALL_VOICE=false      # whether to install voice/TTS extras
DM20_BINARY_PATH=""      # user mode: resolved path to dm20-protocol binary
ON_ICLOUD=false          # true if target dir is on iCloud Drive
MODEL_PROFILE="balanced" # model quality profile: quality, balanced, economy

# ─── Colors ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# ─── Helpers ───────────────────────────────────────────────────────────────────

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()    { echo -e "\n${CYAN}${BOLD}>>> $*${NC}"; }

prompt_default() {
    local prompt="$1"
    local default="$2"
    local varname="$3"
    echo -en "${prompt} ${DIM}[${default}]${NC}: "
    read -r input
    printf -v "$varname" '%s' "${input:-$default}"
}

prompt_yn() {
    local prompt="$1"
    local default="$2"  # y or n
    local varname="$3"
    if [[ "$default" == "y" ]]; then
        echo -en "${prompt} ${DIM}[Y/n]${NC}: "
    else
        echo -en "${prompt} ${DIM}[y/N]${NC}: "
    fi
    read -r input
    input="${input:-$default}"
    if [[ "${input,,}" == "y" || "${input,,}" == "yes" ]]; then
        eval "$varname=true"
    else
        eval "$varname=false"
    fi
}

prompt_choice() {
    local prompt="$1"
    shift
    local options=("$@")
    echo -e "\n${prompt}" >&2
    for i in "${!options[@]}"; do
        echo -e "  ${BOLD}$((i+1)))${NC} ${options[$i]}" >&2
    done
    echo -en "\nChoice: " >&2
    read -r choice
    echo "$choice"
}

# Reject system paths where a git clone would be dangerous or nonsensical
validate_install_path() {
    local dir="$1"

    # Normalize: strip trailing slashes, resolve ~ to $HOME
    dir="${dir%/}"
    dir="${dir/#\~/$HOME}"

    case "$dir" in
        /|/usr|/usr/local|/bin|/sbin|/etc|/var|/tmp|/opt|/System|/Library|/private)
            error "Cannot install to system directory: ${dir}"
            exit 1
            ;;
        "$HOME")
            error "Cannot install directly into your home directory."
            echo "  Try: ${HOME}/dm20-protocol"
            exit 1
            ;;
    esac
}

# ─── Banner ────────────────────────────────────────────────────────────────────

# ─── Terminal Background Detection ────────────────────────────────────────────

detect_terminal_background() {
    # Returns: "dark", "light", or "unknown"

    # Method 1: COLORFGBG variable (set by iTerm2, rxvt, many terminals)
    # Format: "fg;bg" — values 0-7 are dark colors, 8-15 are bright
    if [[ -n "${COLORFGBG:-}" ]]; then
        local bg_value="${COLORFGBG##*;}"
        if [[ "$bg_value" =~ ^[0-9]+$ ]]; then
            if [[ "$bg_value" -lt 8 ]]; then
                echo "dark"; return
            else
                echo "light"; return
            fi
        fi
    fi

    # Method 2: OSC 11 query (asks terminal for background RGB)
    if [ -t 0 ] && [ -t 1 ]; then
        local old_settings response
        old_settings=$(stty -g 2>/dev/null) || true
        stty raw -echo min 0 time 3 2>/dev/null || true
        printf '\033]11;?\033\\' > /dev/tty 2>/dev/null
        response=$(dd bs=1 count=50 2>/dev/null < /dev/tty) || true
        stty "$old_settings" 2>/dev/null || true

        # Parse rgb:RRRR/GGGG/BBBB response
        if [[ "$response" =~ rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+) ]]; then
            local r g b luminance
            r=$(( 16#${BASH_REMATCH[1]:0:2} ))
            g=$(( 16#${BASH_REMATCH[2]:0:2} ))
            b=$(( 16#${BASH_REMATCH[3]:0:2} ))
            # Perceived luminance (ITU-R BT.601)
            luminance=$(( (r * 299 + g * 587 + b * 114) / 1000 ))
            if [[ "$luminance" -gt 128 ]]; then
                echo "light"; return
            else
                echo "dark"; return
            fi
        fi
    fi

    echo "unknown"
}

setup_banner_colors() {
    local theme="$1"
    # Use $'...' syntax so variables hold actual ANSI escape bytes.
    # This avoids conflicts between art backslashes and echo -e interpretation.
    B_NC=$'\033[0m'
    B_BOLD=$'\033[1m'
    B_DIM=$'\033[2m'

    case "$theme" in
        dark)
            # 256-color palette: vivid colors for dark backgrounds
            B_C1=$'\033[1;31m'        # bold red
            B_C2=$'\033[38;5;196m'    # bright red
            B_C3=$'\033[38;5;160m'    # red
            B_C4=$'\033[38;5;202m'    # red-orange
            B_C5=$'\033[38;5;202m'    # red-orange
            B_C6=$'\033[38;5;208m'    # orange
            B_C7=$'\033[38;5;214m'    # light orange
            B_SWORD=$'\033[1;31m'     # bold red
            B_TITLE=$'\033[1;37m'     # bold white
            ;;
        light)
            # 256-color palette: darker, saturated for light backgrounds
            B_C1=$'\033[38;5;124m'    # dark red
            B_C2=$'\033[38;5;160m'    # medium red
            B_C3=$'\033[38;5;160m'    # medium red
            B_C4=$'\033[38;5;166m'    # dark red-orange
            B_C5=$'\033[38;5;166m'    # dark red-orange
            B_C6=$'\033[38;5;172m'    # dark orange
            B_C7=$'\033[38;5;130m'    # brown-orange
            B_SWORD=$'\033[38;5;124m' # dark red
            B_TITLE=$'\033[1;30m'     # bold black
            ;;
        *)
            # ANSI-only fallback: standard colors adapt to any terminal theme
            B_C1=$'\033[1;31m'        # bold red
            B_C2=$'\033[0;31m'        # red
            B_C3=$'\033[0;31m'        # red
            B_C4=$'\033[1;33m'        # bold yellow (≈ orange)
            B_C5=$'\033[1;33m'        # bold yellow
            B_C6=$'\033[0;33m'        # yellow/orange
            B_C7=$'\033[0;33m'        # yellow/orange
            B_SWORD=$'\033[1;31m'     # bold red
            B_TITLE=$'\033[1m'        # bold (default fg)
            ;;
    esac
}

banner() {
    local bg_theme
    bg_theme=$(detect_terminal_background)
    setup_banner_colors "$bg_theme"

    echo ""
    echo "${B_SWORD}  ⚔═══════════════════════════════════════════════════════⚔${B_NC}"
    echo "${B_C1}oooooooooo.   ooo        ooooo   .oooo.     .oooo.${B_NC}"
    echo "${B_C2}\`888'   \`Y8b  \`88.       .888' .dP\"\"Y88b   d8P'\`Y8b${B_NC}"
    echo "${B_C3} 888      888  888b     d'888        ]8P' 888    888${B_NC}"
    echo "${B_C4} 888      888  8 Y88. .P  888      .d8P'  888    888${B_NC}"
    echo "${B_C5} 888      888  8  \`888'   888    .dP'     888    888${B_NC}"
    echo "${B_C6} 888     d88'  8    Y     888  .oP     .o \`88b  d88'${B_NC}"
    echo "${B_C7}o888bood8P'   o8o        o888o 8888888888  \`Y8bd8P'${B_NC}"
    echo "${B_SWORD}  ⚔═══════════════════════════════════════════════════════⚔${B_NC}"
    echo "  ${B_BOLD}${B_TITLE}DM20 Protocol${B_NC} v${VERSION} — ${B_DIM}Installation Wizard${B_NC}"
    echo "  ${B_DIM}AI-powered D&D campaign management via MCP${B_NC}"
    echo ""
}

# ─── Platform Detection ───────────────────────────────────────────────────────

detect_platform() {
    ARCH=$(uname -m)    # x86_64, arm64, aarch64
    OS=$(uname -s)      # Darwin, Linux

    if [[ "$OS" == "Darwin" && "$ARCH" == "x86_64" ]]; then
        PLATFORM="macos-intel"
        RAG_SUPPORTED=true
        RAG_WARNING="macOS Intel: ML libraries may require Python 3.12 (auto-fallback enabled)"
    elif [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
        PLATFORM="macos-arm"
        RAG_SUPPORTED=true
        RAG_WARNING=""
    elif [[ "$OS" == "Linux" ]]; then
        PLATFORM="linux-${ARCH}"
        RAG_SUPPORTED=true
        RAG_WARNING=""
    elif [[ "$OS" == MINGW* || "$OS" == CYGWIN* || "$OS" == MSYS* ]]; then
        error "Windows is not supported by this installer."
        echo "  See the manual installation instructions in README.md"
        exit 1
    else
        PLATFORM="unknown"
        RAG_SUPPORTED=true
        RAG_WARNING=""
    fi

    info "Platform: ${BOLD}${OS} ${ARCH}${NC} (${PLATFORM})"
}

# ─── Mode Detection ──────────────────────────────────────────────────────────

detect_mode() {
    step "Detecting install mode"

    # If running from inside an existing clone → auto-set developer
    if [[ -f "pyproject.toml" ]] && grep -q 'name = "dm20-protocol"' pyproject.toml 2>/dev/null; then
        INSIDE_CLONE=true
        INSTALL_MODE="developer"
        info "Running from inside a dm20-protocol clone — Developer mode selected"
        return
    fi

    echo ""
    echo -e "${BOLD}How do you want to install DM20 Protocol?${NC}"
    echo ""
    local choice
    choice=$(prompt_choice "Choose installation mode:" \
        "User (recommended) — just play D&D, minimal footprint" \
        "Developer — full source code for contributors")

    case "$choice" in
        1) INSTALL_MODE="user" ;;
        2) INSTALL_MODE="developer" ;;
        *) INSTALL_MODE="user" ;;
    esac

    info "Install mode: ${BOLD}${INSTALL_MODE}${NC}"
}

# ─── iCloud Detection ─────────────────────────────────────────────────────────

detect_icloud() {
    local check_dir
    if [[ "$INSTALL_MODE" == "user" ]]; then
        check_dir="$PLAY_DIR"
    else
        check_dir="$INSTALL_DIR"
    fi

    # Check if target dir is inside iCloud Drive
    local resolved_dir
    resolved_dir=$(cd "$check_dir" 2>/dev/null && pwd -P || echo "$check_dir")

    if [[ "$resolved_dir" == *"com~apple~CloudDocs"* ]] || \
       [[ "$resolved_dir" == *"Mobile Documents"* ]]; then
        ON_ICLOUD=true
    elif [[ "$OS" == "Darwin" ]]; then
        # Check if Desktop or Documents are iCloud-synced
        local real_desktop real_documents
        real_desktop=$(cd "$HOME/Desktop" 2>/dev/null && pwd -P 2>/dev/null || echo "")
        real_documents=$(cd "$HOME/Documents" 2>/dev/null && pwd -P 2>/dev/null || echo "")

        if [[ -n "$real_desktop" && "$real_desktop" == *"com~apple~CloudDocs"* && \
              "$resolved_dir" == "$HOME/Desktop"* ]]; then
            ON_ICLOUD=true
        elif [[ -n "$real_documents" && "$real_documents" == *"com~apple~CloudDocs"* && \
                "$resolved_dir" == "$HOME/Documents"* ]]; then
            ON_ICLOUD=true
        fi
    fi

    if [[ "$ON_ICLOUD" == true ]]; then
        warn "Install directory is on iCloud Drive"
        if [[ "$INSTALL_MODE" == "developer" ]]; then
            info "Will protect .venv from iCloud sync (via .nosync + symlink)"
        else
            info "Data files will sync via iCloud (this is fine for campaign data)"
        fi
    fi
}

setup_venv_nosync() {
    # Create .venv.nosync + symlink so iCloud doesn't set UF_HIDDEN on venv files.
    # This prevents Python from skipping .pth files (editable installs).
    # See: https://github.com/pypa/setuptools/issues/4595
    local venv_link="${INSTALL_DIR}/.venv"
    local venv_real="${INSTALL_DIR}/.venv.nosync"

    # Already set up correctly
    if [[ -L "$venv_link" && -d "$venv_real" ]]; then
        success "iCloud protection already in place (.venv → .venv.nosync)"
        return 0
    fi

    # Existing .venv is a real directory — iCloud has been tainting it.
    # We must delete and recreate from scratch so iCloud never indexes the new files.
    # A simple move would preserve iCloud's hidden flags.
    if [[ -d "$venv_link" && ! -L "$venv_link" ]]; then
        info "Removing iCloud-tainted .venv (will be recreated by uv sync)..."
        rm -rf "$venv_link"
    fi

    # Create the nosync directory and symlink
    mkdir -p "$venv_real"
    ln -sf ".venv.nosync" "$venv_link"
    success "iCloud protection: .venv → .venv.nosync (iCloud will ignore it)"
}

# ─── Auto-install Helpers ─────────────────────────────────────────────────────

install_homebrew() {
    info "Installing Homebrew..."
    if /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
        # Make brew available in the current session
        if [[ -x "/opt/homebrew/bin/brew" ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x "/usr/local/bin/brew" ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        if command -v brew &>/dev/null; then
            HAS_BREW=true
            success "Homebrew installed successfully"
            return 0
        else
            error "Homebrew was installed but not found in PATH"
            return 1
        fi
    else
        error "Failed to install Homebrew"
        return 1
    fi
}

install_uv() {
    if [[ "$OS" == "Darwin" && "$HAS_BREW" == true ]]; then
        info "Installing uv via Homebrew..."
        if brew install uv; then
            success "uv installed via Homebrew"
            return 0
        else
            error "Failed to install uv via Homebrew"
            return 1
        fi
    else
        info "Installing uv via official installer..."
        if curl -LsSf https://astral.sh/uv/install.sh | sh; then
            # Make uv available in the current session
            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
            if command -v uv &>/dev/null; then
                success "uv installed successfully"
                return 0
            else
                error "uv was installed but not found in PATH"
                return 1
            fi
        else
            error "Failed to install uv"
            return 1
        fi
    fi
}

install_python() {
    info "Installing Python 3.12 via uv..."
    if uv python install 3.12; then
        success "Python 3.12 installed via uv"
        return 0
    else
        error "Failed to install Python 3.12"
        return 1
    fi
}

install_git() {
    if [[ "$OS" == "Darwin" && "$HAS_BREW" == true ]]; then
        info "Installing git via Homebrew..."
        if brew install git; then
            success "git installed via Homebrew"
            return 0
        else
            error "Failed to install git via Homebrew"
            return 1
        fi
    elif [[ "$OS" == "Darwin" ]]; then
        info "Installing git via Xcode Command Line Tools..."
        xcode-select --install 2>/dev/null || true
        echo ""
        echo -en "  Press ${BOLD}Enter${NC} when Xcode CLI tools installation is complete... "
        read -r
        if command -v git &>/dev/null; then
            return 0
        else
            error "git still not found after Xcode CLI tools install"
            return 1
        fi
    elif command -v apt-get &>/dev/null; then
        info "Installing git via apt..."
        sudo apt-get update && sudo apt-get install -y git
    elif command -v dnf &>/dev/null; then
        info "Installing git via dnf..."
        sudo dnf install -y git
    elif command -v pacman &>/dev/null; then
        info "Installing git via pacman..."
        sudo pacman -S --noconfirm git
    elif command -v zypper &>/dev/null; then
        info "Installing git via zypper..."
        sudo zypper install -y git
    elif command -v apk &>/dev/null; then
        info "Installing git via apk..."
        sudo apk add git
    else
        error "Cannot auto-install git on this system"
        echo "  Install git manually, then re-run this script"
        return 1
    fi
}

# ─── Prerequisites ─────────────────────────────────────────────────────────────

check_prerequisites() {
    step "Checking prerequisites"
    local missing=0

    # ── Homebrew (macOS only) ──────────────────────────────────────────────
    HAS_BREW=false
    if [[ "$OS" == "Darwin" ]]; then
        if command -v brew &>/dev/null; then
            HAS_BREW=true
            local brew_ver
            brew_ver=$(brew --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
            success "Homebrew ${brew_ver}"
        else
            warn "Homebrew not found"
            echo ""
            echo -e "  ${BOLD}Homebrew${NC} is the standard package manager for macOS."
            echo "  It makes installing and updating developer tools effortless."
            echo -e "  ${DIM}https://brew.sh${NC}"
            echo ""
            prompt_yn "  Install Homebrew now?" "y" INSTALL_BREW
            if [[ "$INSTALL_BREW" == true ]]; then
                if install_homebrew; then
                    : # success already printed by install_homebrew
                else
                    info "Continuing without Homebrew (using alternative installers)"
                fi
            else
                info "Skipping Homebrew (will use alternative installers)"
            fi
        fi
    fi

    # ── uv (always needed — both modes) ──────────────────────────────────
    if command -v uv &>/dev/null; then
        UV_VERSION=$(uv --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        UV_PATH=$(command -v uv)
        success "uv ${UV_VERSION} (${UV_PATH})"
    else
        warn "uv not found"
        local uv_method="official installer"
        [[ "$OS" == "Darwin" && "$HAS_BREW" == true ]] && uv_method="Homebrew"
        prompt_yn "  Install uv now? (via ${uv_method})" "y" INSTALL_UV
        if [[ "$INSTALL_UV" == true ]]; then
            if install_uv; then
                UV_VERSION=$(uv --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
                UV_PATH=$(command -v uv)
                success "uv ${UV_VERSION} (${UV_PATH})"
            else
                echo "  Manual install: curl -LsSf https://astral.sh/uv/install.sh | sh"
                missing=1
            fi
        else
            echo "  Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
            missing=1
        fi
    fi

    # ── Python 3.12+ (developer mode only) ────────────────────────────────
    if [[ "$INSTALL_MODE" == "developer" ]]; then
        local python_ok=false
        if command -v python3 &>/dev/null; then
            PYTHON_VERSION=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
            PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
            PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
            if [[ "$PYTHON_MAJOR" -ge 3 && "$PYTHON_MINOR" -ge 12 ]]; then
                success "Python ${PYTHON_VERSION}"
                python_ok=true
            fi
        fi

        if [[ "$python_ok" == false ]]; then
            if [[ -n "${PYTHON_VERSION:-}" ]]; then
                warn "Python ${PYTHON_VERSION} found, but 3.12+ required"
            else
                warn "Python not found"
            fi

            # Can only auto-install if uv is available
            if command -v uv &>/dev/null; then
                prompt_yn "  Install Python 3.12 via uv?" "y" INSTALL_PYTHON
                if [[ "$INSTALL_PYTHON" == true ]]; then
                    if install_python; then
                        success "Python 3.12 available (managed by uv)"
                    else
                        echo "  Install from https://python.org or use pyenv"
                        missing=1
                    fi
                else
                    echo "  Install from https://python.org or use pyenv"
                    missing=1
                fi
            else
                echo "  Install from https://python.org or use pyenv"
                echo "  (uv could install Python for you, but uv is not available)"
                missing=1
            fi
        fi
    fi

    # ── git (developer mode only) ─────────────────────────────────────────
    if [[ "$INSTALL_MODE" == "developer" ]]; then
        if command -v git &>/dev/null; then
            GIT_VERSION=$(git --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            success "git ${GIT_VERSION}"
        else
            warn "git not found"
            prompt_yn "  Install git now?" "y" INSTALL_GIT
            if [[ "$INSTALL_GIT" == true ]]; then
                if install_git; then
                    GIT_VERSION=$(git --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
                    success "git ${GIT_VERSION}"
                else
                    if [[ "$OS" == "Darwin" ]]; then
                        echo "  Install with: xcode-select --install"
                    else
                        echo "  Install with your package manager (apt, dnf, etc.)"
                    fi
                    missing=1
                fi
            else
                if [[ "$OS" == "Darwin" ]]; then
                    echo "  Install with: xcode-select --install"
                else
                    echo "  Install with your package manager (apt, dnf, etc.)"
                fi
                missing=1
            fi
        fi
    fi

    if [[ "$missing" -gt 0 ]]; then
        echo ""
        error "Missing prerequisites. Install them and re-run this script."
        exit 1
    fi
}

# ─── Prompts ───────────────────────────────────────────────────────────────────

gather_options() {
    step "Configuration"

    if [[ "$INSTALL_MODE" == "user" ]]; then
        gather_options_user
    else
        gather_options_developer
    fi
}

gather_options_user() {
    # ── Play directory ────────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}Where should DM20 set up your play directory?${NC}"
    echo -e "  ${DIM}This is where config and campaign data will live${NC}"
    prompt_default "Play directory" "$HOME/dm20" PLAY_DIR
    PLAY_DIR="${PLAY_DIR/#\~/$HOME}"

    # Ensure PLAY_DIR is an absolute path
    if [[ ! "$PLAY_DIR" == /* ]]; then
        PLAY_DIR="$(pwd)/${PLAY_DIR}"
    fi

    # Validate the path is reasonable
    if [[ -z "$PLAY_DIR" || ${#PLAY_DIR} -lt 3 ]]; then
        error "Invalid play directory: '${PLAY_DIR}'"
        exit 1
    fi

    # Auto-append /dm20-protocol if the final directory doesn't reference dm20
    local play_basename
    play_basename="$(basename "$PLAY_DIR")"
    if ! echo "$play_basename" | grep -qi "dm20"; then
        PLAY_DIR="${PLAY_DIR}/dm20"
        info "Auto-appended 'dm20' → ${PLAY_DIR}"
    fi

    DATA_DIR="${PLAY_DIR}/data"

    # ── MCP Client ────────────────────────────────────────────────────────
    echo ""
    local choice
    choice=$(prompt_choice "Which MCP client(s) will you use?" \
        "Claude Desktop" \
        "Claude Code" \
        "Both")
    case "$choice" in
        1) MCP_CLIENT="desktop" ;;
        2) MCP_CLIENT="code" ;;
        3) MCP_CLIENT="both" ;;
        *) MCP_CLIENT="desktop" ;;
    esac
    CODE_SCOPE="project"  # always project-scoped in user mode

    # ── Model Quality Profile ─────────────────────────────────────────────
    echo ""
    local profile_choice
    profile_choice=$(prompt_choice "Model quality profile:" \
        "Balanced — Sonnet models, good quality [recommended]" \
        "Quality — Opus models, best narrative (uses more tokens)" \
        "Economy — Haiku models, fast and cheap (great for Pro plan)")
    case "$profile_choice" in
        2) MODEL_PROFILE="quality" ;;
        3) MODEL_PROFILE="economy" ;;
        *) MODEL_PROFILE="balanced" ;;
    esac

    # ── RAG dependencies ──────────────────────────────────────────────────
    INSTALL_RAG=false
    echo ""
    if [[ "$RAG_SUPPORTED" == false ]]; then
        warn "RAG dependencies skipped: ${RAG_WARNING}"
        echo "  The server works fine without RAG. The Claudmaster AI DM uses it for"
        echo "  module indexing — all other tools (including ask_books) work without it."
    else
        echo -e "${BOLD}Install RAG dependencies?${NC}"
        echo "  Enables vector search for Claudmaster AI DM module indexing (~200MB download)"
        echo "  Not required — all tools including ask_books work without it"
        prompt_yn "Install RAG dependencies?" "n" INSTALL_RAG
    fi

    # ── Voice / TTS dependencies ─────────────────────────────────────────
    INSTALL_VOICE=false
    echo ""
    echo -e "${BOLD}Install Voice Narration (TTS)?${NC}"
    if [[ "$PLATFORM" == "macos-arm" ]]; then
        echo "  Enables AI-narrated gameplay with 3-tier local+cloud TTS:"
        echo "    Tier 1 (speed):   Kokoro 82M — local, <300ms"
        echo "    Tier 2 (quality): mlx-audio  — local, ~800ms"
        echo "    Tier 3 (fallback): Edge-TTS  — cloud, free"
    elif [[ "$PLATFORM" == "macos-intel" ]]; then
        echo "  Enables AI-narrated gameplay via cloud TTS:"
        echo "    Edge-TTS — Microsoft cloud voices, free, no API key"
        echo -e "  ${DIM}Local models (Kokoro, mlx-audio) require Apple Silicon.${NC}"
    else
        echo "  Enables AI-narrated gameplay via cloud TTS:"
        echo "    Edge-TTS — Microsoft cloud voices, free, no API key"
    fi
    echo "  Not required — game works perfectly in text-only mode"
    echo "  Can also be installed later with: bash install.sh --voice"
    prompt_yn "Install Voice/TTS?" "n" INSTALL_VOICE
}

gather_options_developer() {
    # ── Install directory ──────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}Where should DM20 Protocol be installed?${NC}"

    if [[ "$INSIDE_CLONE" == true ]]; then
        INSTALL_DIR="$(pwd)"
        CLONE_NEEDED=false
        info "Using current directory: ${INSTALL_DIR}"
    else
        echo -e "  ${DIM}A dm20-protocol/ directory will be created inside your choice${NC}"
        local parent_dir
        prompt_default "Parent directory" "$HOME" parent_dir
        parent_dir="${parent_dir/#\~/$HOME}"
        parent_dir="${parent_dir%/}"
        INSTALL_DIR="${parent_dir}/dm20-protocol"

        if [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/pyproject.toml" ]] && \
           grep -q 'name = "dm20-protocol"' "$INSTALL_DIR/pyproject.toml" 2>/dev/null; then
            info "Found existing clone at ${INSTALL_DIR}"
            CLONE_NEEDED=false
        else
            CLONE_NEEDED=true
        fi
        validate_install_path "$INSTALL_DIR"
    fi

    # ── MCP Client ────────────────────────────────────────────────────────
    echo ""
    local choice
    choice=$(prompt_choice "Which MCP client(s) will you use?" \
        "Claude Desktop" \
        "Claude Code" \
        "Both")
    case "$choice" in
        1) MCP_CLIENT="desktop" ;;
        2) MCP_CLIENT="code" ;;
        3) MCP_CLIENT="both" ;;
        *) MCP_CLIENT="desktop" ;;
    esac

    # If Claude Code: global or project?
    CODE_SCOPE="global"
    if [[ "$MCP_CLIENT" == "code" || "$MCP_CLIENT" == "both" ]]; then
        echo ""
        local scope_choice
        scope_choice=$(prompt_choice "Claude Code config scope?" \
            "Global (~/.claude/mcp.json) — available in all projects" \
            "Project (.mcp.json) — only in dm20-protocol directory")

        case "$scope_choice" in
            2) CODE_SCOPE="project" ;;
            *) CODE_SCOPE="global" ;;
        esac
    fi

    # ── Data directory ────────────────────────────────────────────────────
    echo ""
    local data_choice
    data_choice=$(prompt_choice "Where should campaign data be stored?" \
        "~/dm20-data (recommended — separate from code)" \
        "Inside the repository (${INSTALL_DIR}/data)" \
        "Custom location")

    case "$data_choice" in
        1) DATA_DIR="$HOME/dm20-data" ;;
        2) DATA_DIR="${INSTALL_DIR}/data" ;;
        3) prompt_default "Data directory" "$HOME/dm20-data" DATA_DIR ;;
        *) DATA_DIR="$HOME/dm20-data" ;;
    esac

    # ── Model Quality Profile ─────────────────────────────────────────────
    echo ""
    local profile_choice
    profile_choice=$(prompt_choice "Model quality profile:" \
        "Balanced — Sonnet models, good quality [recommended]" \
        "Quality — Opus models, best narrative (uses more tokens)" \
        "Economy — Haiku models, fast and cheap (great for Pro plan)")
    case "$profile_choice" in
        2) MODEL_PROFILE="quality" ;;
        3) MODEL_PROFILE="economy" ;;
        *) MODEL_PROFILE="balanced" ;;
    esac

    # ── RAG dependencies ──────────────────────────────────────────────────
    INSTALL_RAG=false
    echo ""
    if [[ "$RAG_SUPPORTED" == false ]]; then
        warn "RAG dependencies skipped: ${RAG_WARNING}"
        echo "  The server works fine without RAG. The Claudmaster AI DM uses it for"
        echo "  module indexing — all other tools (including ask_books) work without it."
    else
        echo -e "${BOLD}Install RAG dependencies?${NC}"
        echo "  Enables vector search for Claudmaster AI DM module indexing (~200MB download)"
        echo "  Not required — all tools including ask_books work without it"
        prompt_yn "Install RAG dependencies?" "n" INSTALL_RAG
    fi

    # ── Voice / TTS dependencies ─────────────────────────────────────────
    INSTALL_VOICE=false
    echo ""
    echo -e "${BOLD}Install Voice Narration (TTS)?${NC}"
    if [[ "$PLATFORM" == "macos-arm" ]]; then
        echo "  Enables AI-narrated gameplay with 3-tier local+cloud TTS:"
        echo "    Tier 1 (speed):   Kokoro 82M — local, <300ms"
        echo "    Tier 2 (quality): mlx-audio  — local, ~800ms"
        echo "    Tier 3 (fallback): Edge-TTS  — cloud, free"
    elif [[ "$PLATFORM" == "macos-intel" ]]; then
        echo "  Enables AI-narrated gameplay via cloud TTS:"
        echo "    Edge-TTS — Microsoft cloud voices, free, no API key"
        echo -e "  ${DIM}Local models (Kokoro, mlx-audio) require Apple Silicon.${NC}"
    else
        echo "  Enables AI-narrated gameplay via cloud TTS:"
        echo "    Edge-TTS — Microsoft cloud voices, free, no API key"
    fi
    echo "  Not required — game works perfectly in text-only mode"
    echo "  Can also be installed later with: bash install.sh --voice"
    prompt_yn "Install Voice/TTS?" "n" INSTALL_VOICE
}

# ─── Installation (User mode) ─────────────────────────────────────────────────

do_tool_install() {
    step "Installing dm20-protocol"

    # ── Build extras string dynamically ──────────────────────────────────
    local extras=""
    [[ "$INSTALL_RAG" == true ]] && extras="rag"
    [[ "$INSTALL_VOICE" == true ]] && extras="${extras:+$extras,}voice"

    local installed=false
    local extras_installed=false

    if [[ -n "$extras" ]]; then
        # ── Attempt 1: with extras, current Python ───────────────────────
        info "Installing with extras [$extras]..."
        if uv tool install "dm20-protocol[$extras] @ git+${REPO_URL}" --force 2>&1; then
            success "dm20-protocol installed with [$extras]"
            installed=true
            extras_installed=true
        else
            # ── Attempt 2: with extras, Python 3.12 ──────────────────────
            warn "Install with [$extras] failed. Retrying with Python 3.12..."
            if uv tool install --python 3.12 "dm20-protocol[$extras] @ git+${REPO_URL}" --force 2>&1; then
                success "dm20-protocol installed with [$extras] (using Python 3.12)"
                installed=true
                extras_installed=true
            else
                # ── Attempt 3: give up on extras ─────────────────────────
                warn "Optional dependencies [$extras] are not available on this platform."
                warn "Installing base package instead."
                INSTALL_RAG=false
                INSTALL_VOICE=false
            fi
        fi
    fi

    if [[ "$installed" == false ]]; then
        # ── Base install (no extras) ─────────────────────────────────────
        info "Installing dm20-protocol (base)..."
        if uv tool install "dm20-protocol @ git+${REPO_URL}" --force 2>&1; then
            success "dm20-protocol installed"
            installed=true
        else
            # ── Last resort: pin Python 3.12 ──────────────────────────────
            warn "Install failed with default Python. Retrying with Python 3.12..."
            if uv tool install --python 3.12 "dm20-protocol @ git+${REPO_URL}" --force 2>&1; then
                success "dm20-protocol installed (using Python 3.12)"
                installed=true
            else
                error "Installation failed. Please check the error output above."
                exit 1
            fi
        fi
    fi

    # Resolve the binary path (needed for MCP config)
    if command -v dm20-protocol &>/dev/null; then
        DM20_BINARY_PATH=$(command -v dm20-protocol)
    elif [[ -x "$HOME/.local/bin/dm20-protocol" ]]; then
        DM20_BINARY_PATH="$HOME/.local/bin/dm20-protocol"
        warn "dm20-protocol not in PATH — adding \$HOME/.local/bin to your shell profile"
        warn "is recommended but not required (MCP config uses the absolute path)."
    else
        error "dm20-protocol binary not found after installation"
        exit 1
    fi
}

do_create_play_dir() {
    step "Setting up play directory at ${PLAY_DIR}"

    # Create the base directory first with explicit error handling
    if ! mkdir -p "${PLAY_DIR}" 2>/dev/null; then
        error "Cannot create play directory: ${PLAY_DIR}"
        error "Check that the parent directory exists and is writable."
        exit 1
    fi

    mkdir -p "${PLAY_DIR}/data/campaigns"
    mkdir -p "${PLAY_DIR}/data/library/pdfs"
    mkdir -p "${PLAY_DIR}/data/library/index"
    mkdir -p "${PLAY_DIR}/data/library/extracted"
    mkdir -p "${PLAY_DIR}/.claude/agents"

    # Copy agent template files from the installed package into the play dir
    # so that /dm:profile can modify them at runtime
    info "Copying CC agent templates to play directory..."
    local agents_src
    agents_src=$(python3 -c "
import importlib.resources as pkg_resources
try:
    # dm20-protocol ships agent templates in the package data
    import dm20_protocol
    import os
    pkg_dir = os.path.dirname(dm20_protocol.__file__)
    # Walk up to find .claude/agents/ in the installed tree
    base = pkg_dir
    for _ in range(5):
        candidate = os.path.join(base, '.claude', 'agents')
        if os.path.isdir(candidate):
            print(candidate)
            break
        base = os.path.dirname(base)
except Exception:
    pass
" 2>/dev/null)

    if [[ -n "$agents_src" && -d "$agents_src" ]]; then
        cp -n "$agents_src"/*.md "${PLAY_DIR}/.claude/agents/" 2>/dev/null || true
        success "Agent templates copied from installed package"
    elif [[ "$INSIDE_CLONE" == true ]]; then
        # Copy from local clone
        local script_dir_agents
        script_dir_agents="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -d "${script_dir_agents}/.claude/agents" ]]; then
            cp -n "${script_dir_agents}/.claude/agents"/*.md "${PLAY_DIR}/.claude/agents/" 2>/dev/null || true
            success "Agent templates copied from local clone"
        fi
    else
        # Download from GitHub (uv tool install doesn't include .claude/ files)
        info "Downloading agent templates from GitHub..."
        local agents_ok=true
        for agent_file in narrator.md combat-handler.md rules-lookup.md python-pro.md; do
            local target="${PLAY_DIR}/.claude/agents/${agent_file}"
            if [[ ! -f "$target" ]]; then
                if ! curl -fsSL "${RAW_BASE}/.claude/agents/${agent_file}" -o "$target" 2>/dev/null; then
                    agents_ok=false
                    # Fallback: create minimal template
                    local agent_name="${agent_file%.md}"
                    local model="opus" effort="medium"
                    [[ "$agent_name" == "rules-lookup" ]] && model="haiku" && effort="low"
                    [[ "$agent_name" == "python-pro" ]] && model="sonnet"
                    cat > "$target" << AGENTEOF
---
name: ${agent_name}
model: ${model}
effort: ${effort}
---
AGENTEOF
                fi
            fi
        done
        if [[ "$agents_ok" == true ]]; then
            success "Agent templates downloaded from GitHub"
        else
            warn "Some agent templates could not be downloaded — minimal templates created instead."
        fi
    fi

    # ── Plugin mode: commands, persona and hooks come from the "dm" plugin ─────
    if [[ "$PLUGIN_MODE" == true ]]; then
        if do_install_plugin "$PLAY_DIR"; then
            return 0
        fi
        warn "Falling back to copying the commands into ${PLAY_DIR}/.claude/"
    fi

    # ── Slash commands (/dm:*) and DM persona ──────────────────────────────────
    # These files live at repo root (.claude/), not inside the Python package,
    # so they are NOT included in uv tool install. We must fetch them separately.

    mkdir -p "${PLAY_DIR}/.claude/commands/dm"

    local commands_src=""

    # Try 1: copy from local clone (if running from repo)
    if [[ "$INSIDE_CLONE" == true ]]; then
        local script_dir
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -d "${script_dir}/.claude/commands/dm" ]]; then
            commands_src="${script_dir}/.claude"
        fi
    fi

    if [[ -n "$commands_src" ]]; then
        info "Copying slash commands from local clone..."
        cp -n "$commands_src"/commands/dm/*.md "${PLAY_DIR}/.claude/commands/dm/" 2>/dev/null || true
        [[ -f "$commands_src/dm-persona.md" ]] && cp -n "$commands_src/dm-persona.md" "${PLAY_DIR}/.claude/" 2>/dev/null || true
        success "Slash commands and DM persona copied from local clone"
    else
        # Try 2: download from GitHub
        info "Downloading slash commands from GitHub..."
        local dm_commands=(start action combat save profile help campaigns install-rag refrill party-auto party-kick party-mode party-next party-status party-stop party-token release-notes)
        local download_ok=true

        for cmd in "${dm_commands[@]}"; do
            local target="${PLAY_DIR}/.claude/commands/dm/${cmd}.md"
            if [[ ! -f "$target" ]]; then
                if ! curl -fsSL "${RAW_BASE}/.claude/commands/dm/${cmd}.md" -o "$target" 2>/dev/null; then
                    download_ok=false
                fi
            fi
        done

        # DM persona file (referenced by all commands via: !`cat .claude/dm-persona.md`)
        local persona_target="${PLAY_DIR}/.claude/dm-persona.md"
        if [[ ! -f "$persona_target" ]]; then
            if ! curl -fsSL "${RAW_BASE}/.claude/dm-persona.md" -o "$persona_target" 2>/dev/null; then
                download_ok=false
            fi
        fi

        if [[ "$download_ok" == true ]]; then
            success "Slash commands and DM persona downloaded from GitHub"
        else
            warn "Some files could not be downloaded. /dm: commands may not work."
            warn "You can copy them manually from the repo: .claude/commands/dm/ and .claude/dm-persona.md"
        fi
    fi

    # ── Project settings (hooks for session management) ────────────────────
    info "Adding PreCompact checkpoint + SessionStart resume hooks to project settings..."
    write_dm20_hooks "${PLAY_DIR}/.claude/settings.json" install
    success "Project settings updated"
}

# ─── Session hooks (legacy copy mode) ──────────────────────────────────────────
# PreCompact runs dm20-checkpoint, which writes a session note from stored game
# state and .claude/last-campaign.txt; SessionStart(compact) reads that file and
# tells Claude to resume. Only dm20's own entries are added or removed; any other
# settings and hooks in the file are preserved.

write_dm20_hooks() {
    local settings_file="$1"
    local action="$2"   # "install" or "remove"
    mkdir -p "$(dirname "$settings_file")"
    python3 - "$settings_file" "$action" << 'PYEOF'
import json
import sys
from pathlib import Path

path, action = Path(sys.argv[1]), sys.argv[2]

PRECOMPACT = (
    "bash -c 'if command -v dm20-checkpoint >/dev/null 2>&1; then dm20-checkpoint 2>/dev/null; "
    "else echo \"dm20: dm20-checkpoint not found, so no automatic checkpoint was written. "
    "Run /dm:save to keep a session note.\"; fi; exit 0'"
)
RESUME = (
    "bash -c 'f=\".claude/last-campaign.txt\"; if [ -f \"$f\" ]; then campaign=$(cat \"$f\"); "
    "rm -f \"$f\"; echo \"🎲 Session checkpoint detected for campaign: $campaign. "
    "Auto-resuming — invoke /dm:start $campaign now.\"; fi'"
)
MARKERS = ("dm20", "last-campaign.txt", "/dm:refrill")


def is_ours(group):
    return any(
        any(m in str(h.get("command", "")) for m in MARKERS)
        for h in group.get("hooks", [])
    )


try:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
except ValueError:
    backup = path.with_suffix(".json.invalid")
    path.replace(backup)
    print(f"  Existing {path.name} was not valid JSON; moved to {backup.name}")
    settings = {}

hooks = settings.setdefault("hooks", {})
for event in ("PreCompact", "SessionStart"):
    kept = [g for g in hooks.get(event, []) if isinstance(g, dict) and not is_ours(g)]
    if action == "install":
        if event == "PreCompact":
            kept.append({"hooks": [{"type": "command", "command": PRECOMPACT, "timeout": 120}]})
        else:
            kept.append({"matcher": "compact", "hooks": [{"type": "command", "command": RESUME}]})
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
if not hooks:
    settings.pop("hooks", None)

if settings or path.is_file():
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PYEOF
}

# ─── Plugin mode ───────────────────────────────────────────────────────────────
# Installs the "dm" Claude Code plugin from plugin/ in a local clone. Commands,
# persona and hooks then live in the plugin; agent templates are still copied
# into the play directory because /dm:profile rewrites their model/effort lines.

resolve_plugin_source() {
    # Prints the directory that holds plugin/.claude-plugin/marketplace.json.
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
    if [[ -n "$script_dir" && -f "${script_dir}/plugin/.claude-plugin/marketplace.json" ]]; then
        echo "${script_dir}/plugin"
        return 0
    fi
    if ! command -v git &>/dev/null; then
        return 1
    fi
    if [[ -d "${PLUGIN_SRC_DIR}/.git" ]]; then
        git -C "$PLUGIN_SRC_DIR" pull --ff-only --quiet >/dev/null 2>&1 || true
    else
        mkdir -p "$(dirname "$PLUGIN_SRC_DIR")"
        git clone --depth 1 --quiet "$REPO_URL" "$PLUGIN_SRC_DIR" >/dev/null 2>&1 || return 1
    fi
    [[ -f "${PLUGIN_SRC_DIR}/plugin/.claude-plugin/marketplace.json" ]] || return 1
    echo "${PLUGIN_SRC_DIR}/plugin"
}

do_install_plugin() {
    local play_dir="$1"
    step "Installing the '${PLUGIN_NAME}' Claude Code plugin"

    if ! command -v claude &>/dev/null; then
        warn "The 'claude' CLI was not found, so the plugin cannot be installed."
        return 1
    fi

    local plugin_src
    if ! plugin_src="$(resolve_plugin_source)"; then
        warn "Could not find or fetch the plugin sources (needs git)."
        return 1
    fi
    info "Plugin marketplace: ${plugin_src}"

    local known_marketplaces
    known_marketplaces="$(claude plugin marketplace list 2>/dev/null || true)"
    if grep -q "$PLUGIN_MARKETPLACE" <<<"$known_marketplaces"; then
        claude plugin marketplace update "$PLUGIN_MARKETPLACE" >/dev/null 2>&1 || true
    elif ! claude plugin marketplace add "$plugin_src" >/dev/null 2>&1; then
        warn "claude plugin marketplace add ${plugin_src} failed."
        return 1
    fi

    local plugin_ref="${PLUGIN_NAME}@${PLUGIN_MARKETPLACE}"
    if ! (cd "$play_dir" && claude plugin install "$plugin_ref" --scope project >/dev/null 2>&1); then
        if ! (cd "$play_dir" && claude plugin update "$plugin_ref" --scope project >/dev/null 2>&1); then
            warn "claude plugin install ${plugin_ref} failed."
            return 1
        fi
    fi
    success "Plugin ${plugin_ref} installed for ${play_dir}"

    # Agent templates: project-level, so /dm:profile can rewrite model:/effort:.
    mkdir -p "${play_dir}/.claude/agents"
    cp -n "${plugin_src}/${PLUGIN_NAME}/agent-templates"/*.md "${play_dir}/.claude/agents/" 2>/dev/null || true

    # Retire the legacy copies so commands and hooks aren't defined twice.
    if [[ -d "${play_dir}/.claude/commands/dm" ]]; then
        rm -rf "${play_dir}/.claude/commands/dm"
        info "Removed legacy .claude/commands/dm/ (now provided by the plugin)"
    fi
    rm -f "${play_dir}/.claude/dm-persona.md"
    if [[ -f "${play_dir}/.claude/settings.json" ]]; then
        write_dm20_hooks "${play_dir}/.claude/settings.json" remove
    fi
    return 0
}

# ─── Installation (Developer mode) ────────────────────────────────────────────

do_clone() {
    if [[ "$CLONE_NEEDED" == true ]]; then
        step "Cloning repository"
        if [[ -d "$INSTALL_DIR" ]]; then
            error "Directory ${INSTALL_DIR} already exists but is not a dm20-protocol clone."
            echo "  Remove it or choose a different parent directory and re-run."
            exit 1
        fi
        git clone "$REPO_URL" "$INSTALL_DIR"
        success "Cloned to ${INSTALL_DIR}"
    else
        step "Updating existing clone"
        cd "$INSTALL_DIR"
        if git pull --ff-only 2>/dev/null; then
            success "Updated to latest"
        else
            warn "Could not fast-forward. You may need to manually resolve."
        fi
    fi
}

do_install_deps() {
    step "Installing dependencies"
    cd "$INSTALL_DIR"

    # Protect .venv from iCloud before creating it
    if [[ "$ON_ICLOUD" == true ]]; then
        setup_venv_nosync
    fi

    uv sync
    success "Core dependencies installed"

    # ── Install optional extras (RAG, voice) ────────────────────────────
    local extra_args=""
    [[ "$INSTALL_RAG" == true ]] && extra_args="$extra_args --extra rag"
    [[ "$INSTALL_VOICE" == true ]] && extra_args="$extra_args --extra voice"

    if [[ -n "$extra_args" ]]; then
        info "Installing optional dependencies:$extra_args..."
        # shellcheck disable=SC2086
        if uv sync $extra_args; then
            success "Optional dependencies installed"
        else
            warn "Install failed with default Python. Retrying with Python 3.12..."
            # shellcheck disable=SC2086
            if uv sync --python 3.12 $extra_args; then
                success "Optional dependencies installed (using Python 3.12)"
            else
                warn "Optional dependencies are not available on this platform."
                info "The server will work without them."
                INSTALL_RAG=false
                INSTALL_VOICE=false
                uv sync
            fi
        fi
    fi
}

do_create_data_dirs() {
    step "Setting up data directory"
    mkdir -p "${DATA_DIR}/campaigns"
    mkdir -p "${DATA_DIR}/library/pdfs"
    mkdir -p "${DATA_DIR}/library/index"
    mkdir -p "${DATA_DIR}/library/extracted"
    success "Data directory created at ${DATA_DIR}"
}

do_write_env() {
    step "Writing .env file"
    local env_file="${INSTALL_DIR}/.env"

    if [[ -f "$env_file" ]]; then
        local backup="${env_file}.backup.$(date +%Y%m%d%H%M%S)"
        cp "$env_file" "$backup"
        info "Backed up existing .env to ${backup}"
    fi

    cat > "$env_file" << EOF
DM20_STORAGE_DIR=${DATA_DIR}
EOF
    success ".env written"
}

# ─── MCP Client Configuration ─────────────────────────────────────────────────

update_json_config() {
    local config_file="$1"
    local use_abs_uv="$2"    # true for Desktop, false for Code
    local add_type="$3"      # true for Code, false for Desktop

    # Build the new server entry using Python (no jq dependency)
    python3 << PYEOF
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

config_file = "$config_file"
install_mode = "$INSTALL_MODE"
use_abs_uv = "$use_abs_uv" == "true"
add_type = "$add_type" == "true"
on_icloud = "$ON_ICLOUD" == "true"
model_profile = "$MODEL_PROFILE"

server_entry = {}

if install_mode == "user":
    data_dir = "$DATA_DIR"
    play_dir = "$PLAY_DIR"
    binary_path = "${DM20_BINARY_PATH}"

    if add_type:
        # Claude Code — uses plain command name
        server_entry["type"] = "stdio"
        server_entry["command"] = "dm20-protocol"
    else:
        # Claude Desktop — needs absolute path (no user PATH)
        server_entry["command"] = binary_path if binary_path else "dm20-protocol"

    env = {"DM20_STORAGE_DIR": data_dir}
    # Point to play dir's .claude/agents/ for profile switching
    agents_dir = str(Path(play_dir) / ".claude" / "agents")
    env["DM20_AGENTS_DIR"] = agents_dir
    env["DM20_DEFAULT_PROFILE"] = model_profile
    server_entry["env"] = env

else:
    install_dir = "${INSTALL_DIR}"
    data_dir = "$DATA_DIR"

    # Resolve uv path
    if use_abs_uv:
        uv_cmd = "${UV_PATH:-uv}"
    else:
        uv_cmd = "uv"

    if add_type:
        # Claude Code supports "cwd" field
        server_entry["type"] = "stdio"
        server_entry["command"] = uv_cmd
        server_entry["args"] = ["run", "python", "-m", "dm20_protocol"]
        server_entry["cwd"] = install_dir
    else:
        # Claude Desktop does NOT support "cwd" — use --directory flag instead
        server_entry["command"] = uv_cmd
        server_entry["args"] = ["run", "--directory", install_dir, "python", "-m", "dm20_protocol"]

    env = {"DM20_STORAGE_DIR": data_dir}
    # Developer mode: agents live in repo's .claude/agents/
    env["DM20_AGENTS_DIR"] = str(Path(install_dir) / ".claude" / "agents")
    env["DM20_DEFAULT_PROFILE"] = model_profile
    # Safety net: if on iCloud, add PYTHONPATH to bypass hidden .pth files
    if on_icloud:
        env["PYTHONPATH"] = str(Path(install_dir) / "src")
    server_entry["env"] = env

# Read existing config or create new
config_path = Path(config_file)
if config_path.exists():
    # Create backup
    backup = config_path.with_suffix(f".backup.{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
    shutil.copy2(config_file, backup)
    print(f"  Backed up to {backup}")

    with open(config_file) as f:
        config = json.load(f)
else:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {}

# Merge
if "mcpServers" not in config:
    config["mcpServers"] = {}

config["mcpServers"]["dm20-protocol"] = server_entry

# Write
with open(config_file, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print(f"  Updated {config_file}")
PYEOF
}

do_configure_mcp() {
    step "Configuring MCP client(s)"

    # Claude Desktop
    if [[ "$MCP_CLIENT" == "desktop" || "$MCP_CLIENT" == "both" ]]; then
        local desktop_config
        if [[ "$OS" == "Darwin" ]]; then
            desktop_config="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
        else
            desktop_config="$HOME/.config/Claude/claude_desktop_config.json"
        fi
        info "Configuring Claude Desktop..."
        update_json_config "$desktop_config" true false
        success "Claude Desktop configured"
    fi

    # Claude Code
    if [[ "$MCP_CLIENT" == "code" || "$MCP_CLIENT" == "both" ]]; then
        local code_config
        if [[ "$INSTALL_MODE" == "user" ]]; then
            code_config="${PLAY_DIR}/.mcp.json"
        elif [[ "$CODE_SCOPE" == "global" ]]; then
            code_config="$HOME/.claude/mcp.json"
        else
            code_config="${INSTALL_DIR}/.mcp.json"
        fi
        info "Configuring Claude Code (${CODE_SCOPE})..."
        update_json_config "$code_config" false true
        success "Claude Code configured (${CODE_SCOPE})"
    fi
}

# ─── Verification ──────────────────────────────────────────────────────────────

do_verify() {
    step "Verifying installation"

    if [[ "$INSTALL_MODE" == "user" ]]; then
        if command -v dm20-protocol &>/dev/null || [[ -x "${DM20_BINARY_PATH}" ]]; then
            success "MCP server ready"
        else
            warn "dm20-protocol binary not found — MCP config may not work"
        fi
    else
        cd "$INSTALL_DIR"
        # Smoke test: import the package
        if uv run python3 -c "from dm20_protocol.main import main; print('Import OK')" 2>/dev/null; then
            success "Server module loads correctly"
        else
            warn "Server module failed to load. Check the output above for errors."
        fi
    fi
}

# ─── Summary ───────────────────────────────────────────────────────────────────

print_summary() {
    echo ""
    echo -e "${CYAN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}${BOLD}  Installation complete!${NC}"
    echo -e "${CYAN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    if [[ "$INSTALL_MODE" == "user" ]]; then
        print_summary_user
    else
        print_summary_developer
    fi
}

print_summary_user() {
    echo -e "  ${BOLD}Mode:${NC}         User"
    echo -e "  ${BOLD}Play dir:${NC}     ${PLAY_DIR}"
    echo -e "  ${BOLD}Data dir:${NC}     ${DATA_DIR}"
    echo -e "  ${BOLD}Platform:${NC}     ${PLATFORM}"
    echo -e "  ${BOLD}Profile:${NC}      ${MODEL_PROFILE}"
    echo -e "  ${BOLD}RAG:${NC}          $([ "$INSTALL_RAG" == true ] && echo "Installed" || echo "Skipped")"
    echo -e "  ${BOLD}Voice (TTS):${NC}  $([ "$INSTALL_VOICE" == true ] && echo "Installed" || echo "Skipped")"
    echo -e "  ${BOLD}MCP client:${NC}   ${MCP_CLIENT}"
    echo ""

    echo ""
    echo -e "  ${YELLOW}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${YELLOW}${BOLD}║             ⚡  WHAT'S NEXT  ⚡                      ║${NC}"
    echo -e "  ${YELLOW}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""

    if [[ "$MCP_CLIENT" == "code" || "$MCP_CLIENT" == "both" ]]; then
        echo -e "  ${GREEN}${BOLD}▶ Claude Code:${NC}"
        echo -e "    ${BOLD}cd ${PLAY_DIR} && claude${NC}"
        echo -e "    Then type ${BOLD}/mcp${NC} to verify the connection."
        echo ""
    fi

    if [[ "$MCP_CLIENT" == "desktop" || "$MCP_CLIENT" == "both" ]]; then
        echo -e "  ${GREEN}${BOLD}▶ Claude Desktop:${NC}"
        echo "    Restart Claude Desktop to pick up the new MCP server."
        echo ""
    fi

    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    echo -e "  ${BOLD}Add PDF rulebooks:${NC}"
    echo "    Drop .pdf or .md files into: ${DATA_DIR}/library/pdfs/"
    echo ""
    if [[ "$INSTALL_VOICE" == true ]]; then
        echo -e "  ${BOLD}Enable voice narration:${NC}"
        echo "    Run ${BOLD}/dm:profile${NC} in Claude Code → set mode to Narrated or Immersive"
        echo ""
    fi
    echo -e "  ${BOLD}Update later:${NC}"
    echo "    uv tool upgrade dm20-protocol"
    echo ""
    echo -e "  ${DIM}Documentation: https://github.com/Polloinfilzato/dm20-protocol${NC}"
    echo ""
}

print_summary_developer() {
    echo -e "  ${BOLD}Mode:${NC}         Developer"
    echo -e "  ${BOLD}Install dir:${NC}  ${INSTALL_DIR}"
    echo -e "  ${BOLD}Data dir:${NC}     ${DATA_DIR}"
    echo -e "  ${BOLD}Platform:${NC}     ${PLATFORM}"
    echo -e "  ${BOLD}Profile:${NC}      ${MODEL_PROFILE}"
    echo -e "  ${BOLD}RAG:${NC}          $([ "$INSTALL_RAG" == true ] && echo "Installed" || echo "Skipped")"
    echo -e "  ${BOLD}Voice (TTS):${NC}  $([ "$INSTALL_VOICE" == true ] && echo "Installed" || echo "Skipped")"
    echo -e "  ${BOLD}MCP client:${NC}   ${MCP_CLIENT}"
    if [[ "$ON_ICLOUD" == true ]]; then
        echo -e "  ${BOLD}iCloud:${NC}       Protected (.venv.nosync + PYTHONPATH)"
    fi
    echo ""

    echo ""
    echo -e "  ${YELLOW}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${YELLOW}${BOLD}║             ⚡  WHAT'S NEXT  ⚡                      ║${NC}"
    echo -e "  ${YELLOW}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""

    if [[ "$MCP_CLIENT" == "code" || "$MCP_CLIENT" == "both" ]]; then
        echo -e "  ${GREEN}${BOLD}▶ Claude Code:${NC}"
        echo -e "    Run ${BOLD}/mcp${NC} in Claude Code to verify the connection."
        echo ""
    fi

    if [[ "$MCP_CLIENT" == "desktop" || "$MCP_CLIENT" == "both" ]]; then
        echo -e "  ${GREEN}${BOLD}▶ Claude Desktop:${NC}"
        echo "    Restart Claude Desktop to pick up the new MCP server."
        echo ""
    fi

    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    echo -e "  ${BOLD}Run manually:${NC}"
    echo "    cd ${INSTALL_DIR} && uv run python -m dm20_protocol"
    echo ""
    echo -e "  ${BOLD}Add PDF rulebooks:${NC}"
    echo "    Drop .pdf or .md files into: ${DATA_DIR}/library/pdfs/"
    echo ""
    echo -e "  ${DIM}Documentation: https://github.com/Polloinfilzato/dm20-protocol${NC}"
    echo ""
}

# ─── Upgrade Mode ─────────────────────────────────────────────────────────────
# Usage: bash install.sh --upgrade [play_dir]
# Updates Python package + .claude/ config files in an existing play directory.

is_dm20_play_dir() {
    # A valid DM20 play directory has .mcp.json or .claude/ with dm20 indicators
    local dir="$1"
    [[ -f "$dir/.mcp.json" ]] && grep -q "dm20-protocol" "$dir/.mcp.json" 2>/dev/null && return 0
    [[ -d "$dir/.claude/commands/dm" ]] && return 0
    [[ -d "$dir/.claude/agents" ]] && [[ -d "$dir/data/campaigns" ]] && return 0
    return 1
}

do_upgrade() {
    local play_dir="${1:-}"

    echo ""
    echo -e "${BOLD}DM20 Protocol — Upgrade${NC}"
    echo ""

    # ── Auto-detect play directory ────────────────────────────────────────
    if [[ -n "$play_dir" ]]; then
        # Explicit path provided as argument
        play_dir="${play_dir/#\~/$HOME}"
        [[ ! "$play_dir" == /* ]] && play_dir="$(pwd)/${play_dir}"
    else
        # Try to auto-detect
        local candidates=()

        # 1. Current directory
        if is_dm20_play_dir "$(pwd)"; then
            candidates+=("$(pwd)")
        fi

        # 2. Default location ~/dm20
        if [[ "$(pwd)" != "$HOME/dm20" ]] && is_dm20_play_dir "$HOME/dm20"; then
            candidates+=("$HOME/dm20")
        fi

        # 3. DM20_STORAGE_DIR env var (play dir is its parent)
        if [[ -n "${DM20_STORAGE_DIR:-}" ]]; then
            local env_parent
            env_parent="$(dirname "$DM20_STORAGE_DIR")"
            if is_dm20_play_dir "$env_parent"; then
                local already_found=false
                for c in "${candidates[@]}"; do
                    [[ "$c" == "$env_parent" ]] && already_found=true
                done
                [[ "$already_found" == false ]] && candidates+=("$env_parent")
            fi
        fi

        if [[ ${#candidates[@]} -eq 1 ]]; then
            play_dir="${candidates[0]}"
            info "Found play directory: ${play_dir}"
        elif [[ ${#candidates[@]} -gt 1 ]]; then
            echo -e "${BOLD}Multiple DM20 play directories found:${NC}"
            local i=1
            for c in "${candidates[@]}"; do
                echo "  ${i}) ${c}"
                ((i++))
            done
            echo ""
            local choice
            read -rp "Which one? [1]: " choice
            choice="${choice:-1}"
            play_dir="${candidates[$((choice - 1))]}"
        else
            # Nothing found — ask the user to cd into it
            error "Could not find a DM20 play directory."
            echo ""
            echo -e "  ${BOLD}Option 1:${NC} cd into your play directory and run:"
            echo -e "    ${DIM}cd ~/dm20 && bash install.sh --upgrade${NC}"
            echo ""
            echo -e "  ${BOLD}Option 2:${NC} specify the path explicitly:"
            echo -e "    ${DIM}bash install.sh --upgrade /path/to/your/dm20${NC}"
            echo ""
            exit 1
        fi
    fi

    # Validate
    if [[ ! -d "$play_dir" ]]; then
        error "Play directory not found: ${play_dir}"
        error "Run the full installer first, or check the path."
        exit 1
    fi

    if ! is_dm20_play_dir "$play_dir"; then
        error "Not a valid DM20 play directory: ${play_dir}"
        error "Expected .mcp.json with dm20-protocol config, or .claude/commands/dm/"
        exit 1
    fi

    # ── Step 1: Upgrade Python package ────────────────────────────────────
    step "Upgrading Python package..."
    if command -v uv &>/dev/null; then
        if uv tool upgrade dm20-protocol 2>/dev/null; then
            success "Python package upgraded"
        else
            warn "uv tool upgrade failed — trying reinstall..."
            if uv tool install "dm20-protocol @ git+${REPO_URL}" --force 2>/dev/null; then
                success "Python package reinstalled"
            else
                warn "Retrying with Python 3.12..."
                uv tool install --python 3.12 "dm20-protocol @ git+${REPO_URL}" --force 2>/dev/null && \
                    success "Python package reinstalled (using Python 3.12)" || \
                    warn "Package upgrade failed. Continuing with config update..."
            fi
        fi
    else
        warn "uv not found — skipping Python package upgrade"
        warn "Install uv (https://docs.astral.sh/uv/) or upgrade manually"
    fi

    # ── Step 2: Backup existing .claude/ files ────────────────────────────
    local backup_dir="${play_dir}/.claude/backup-$(date +%Y%m%d-%H%M%S)"
    step "Backing up current config to ${backup_dir}..."
    mkdir -p "$backup_dir"

    # Backup commands, persona, and agents
    [[ -d "${play_dir}/.claude/commands" ]] && cp -r "${play_dir}/.claude/commands" "$backup_dir/" 2>/dev/null || true
    [[ -f "${play_dir}/.claude/dm-persona.md" ]] && cp "${play_dir}/.claude/dm-persona.md" "$backup_dir/" 2>/dev/null || true
    [[ -d "${play_dir}/.claude/agents" ]] && cp -r "${play_dir}/.claude/agents" "$backup_dir/" 2>/dev/null || true
    [[ -f "${play_dir}/.claude/settings.json" ]] && cp "${play_dir}/.claude/settings.json" "$backup_dir/" 2>/dev/null || true
    success "Backup created"

    # A play directory that already uses the plugin stays on the plugin.
    if [[ -f "${play_dir}/.claude/settings.json" ]] && grep -q "${PLUGIN_NAME}@${PLUGIN_MARKETPLACE}" "${play_dir}/.claude/settings.json" 2>/dev/null; then
        PLUGIN_MODE=true
    fi

    # ── Step 3 (plugin mode): update the plugin instead of copying files ───
    local plugin_ok=false
    if [[ "$PLUGIN_MODE" == true ]]; then
        if do_install_plugin "$play_dir"; then
            plugin_ok=true
            # Refresh agent templates (the backup keeps the previous ones).
            local plugin_src
            plugin_src="$(resolve_plugin_source)" && cp "${plugin_src}/${PLUGIN_NAME}/agent-templates"/*.md "${play_dir}/.claude/agents/" 2>/dev/null || true
        else
            warn "Plugin upgrade failed; updating the copied config files instead."
        fi
    fi

    if [[ "$plugin_ok" == false ]]; then
        # ── Step 3: Download latest .claude/ files ────────────────────────────
        local src_dir=""

        # Try local clone first
        if [[ -f "pyproject.toml" ]] && grep -q 'name = "dm20-protocol"' pyproject.toml 2>/dev/null; then
            src_dir="$(pwd)/.claude"
        fi

        # Commands
        mkdir -p "${play_dir}/.claude/commands/dm"

        if [[ -n "$src_dir" && -d "$src_dir/commands/dm" ]]; then
            step "Updating slash commands from local clone..."
            cp "$src_dir"/commands/dm/*.md "${play_dir}/.claude/commands/dm/" 2>/dev/null || true
            [[ -f "$src_dir/dm-persona.md" ]] && cp "$src_dir/dm-persona.md" "${play_dir}/.claude/" 2>/dev/null || true
            cp "$src_dir"/agents/*.md "${play_dir}/.claude/agents/" 2>/dev/null || true
            success "Config files updated from local clone"
        else
            step "Downloading latest config from GitHub..."
            local update_ok=true

            # Slash commands
            local dm_commands=(start action combat save profile help campaigns install-rag refrill party-auto party-kick party-mode party-next party-status party-stop party-token release-notes)
            for cmd in "${dm_commands[@]}"; do
                if ! curl -fsSL "${RAW_BASE}/.claude/commands/dm/${cmd}.md" -o "${play_dir}/.claude/commands/dm/${cmd}.md" 2>/dev/null; then
                    update_ok=false
                fi
            done

            # DM persona
            if ! curl -fsSL "${RAW_BASE}/.claude/dm-persona.md" -o "${play_dir}/.claude/dm-persona.md" 2>/dev/null; then
                update_ok=false
            fi

            # Agent templates
            mkdir -p "${play_dir}/.claude/agents"
            for agent_file in narrator.md combat-handler.md rules-lookup.md python-pro.md; do
                if ! curl -fsSL "${RAW_BASE}/.claude/agents/${agent_file}" -o "${play_dir}/.claude/agents/${agent_file}" 2>/dev/null; then
                    update_ok=false
                fi
            done

            if [[ "$update_ok" == true ]]; then
                success "All config files downloaded from GitHub"
            else
                warn "Some files could not be downloaded."
                warn "Backup preserved at: ${backup_dir}"
            fi
        fi

        # ── Step 4: Update project settings (PreCompact + SessionStart hooks) ──
        step "Updating project settings (PreCompact checkpoint + SessionStart resume hooks)..."
        write_dm20_hooks "${play_dir}/.claude/settings.json" install
        success "Project settings updated (other settings preserved)"
    fi

    # ── Summary ───────────────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}${GREEN}✅ Upgrade complete!${NC}"
    echo ""
    echo -e "  ${BOLD}Play directory:${NC} ${play_dir}"
    echo -e "  ${BOLD}Backup:${NC} ${backup_dir}"
    echo ""
    echo -e "  ${DIM}If something broke, restore from backup:${NC}"
    echo -e "  ${DIM}  cp -r ${backup_dir}/* ${play_dir}/.claude/${NC}"
    echo ""
}

# ─── Voice Mode ───────────────────────────────────────────────────────────────
# Usage: bash install.sh --voice [play_dir]
# Adds voice/TTS dependencies to an existing installation.

do_install_voice() {
    local play_dir="${1:-}"

    echo ""
    echo -e "${BOLD}DM20 Protocol — Voice Install${NC}"
    echo ""

    # ── Auto-detect play directory (same logic as --upgrade) ──────────────
    if [[ -n "$play_dir" ]]; then
        play_dir="${play_dir/#\~/$HOME}"
        [[ ! "$play_dir" == /* ]] && play_dir="$(pwd)/${play_dir}"
    else
        local candidates=()
        if is_dm20_play_dir "$(pwd)"; then
            candidates+=("$(pwd)")
        fi
        if [[ "$(pwd)" != "$HOME/dm20" ]] && is_dm20_play_dir "$HOME/dm20"; then
            candidates+=("$HOME/dm20")
        fi
        if [[ -n "${DM20_STORAGE_DIR:-}" ]]; then
            local env_parent
            env_parent="$(dirname "$DM20_STORAGE_DIR")"
            if is_dm20_play_dir "$env_parent"; then
                local already_found=false
                for c in "${candidates[@]}"; do
                    [[ "$c" == "$env_parent" ]] && already_found=true
                done
                [[ "$already_found" == false ]] && candidates+=("$env_parent")
            fi
        fi

        if [[ ${#candidates[@]} -eq 1 ]]; then
            play_dir="${candidates[0]}"
            info "Found play directory: ${play_dir}"
        elif [[ ${#candidates[@]} -gt 1 ]]; then
            echo -e "${BOLD}Multiple DM20 play directories found:${NC}"
            local i=1
            for c in "${candidates[@]}"; do
                echo "  ${i}) ${c}"
                ((i++))
            done
            echo ""
            local choice
            read -rp "Which one? [1]: " choice
            choice="${choice:-1}"
            play_dir="${candidates[$((choice - 1))]}"
        else
            error "Could not find a DM20 play directory."
            echo ""
            echo -e "  ${BOLD}Option 1:${NC} cd into your play directory and run:"
            echo -e "    ${DIM}cd ~/dm20 && bash install.sh --voice${NC}"
            echo ""
            echo -e "  ${BOLD}Option 2:${NC} specify the path explicitly:"
            echo -e "    ${DIM}bash install.sh --voice /path/to/your/dm20${NC}"
            echo ""
            exit 1
        fi
    fi

    if [[ ! -d "$play_dir" ]] || ! is_dm20_play_dir "$play_dir"; then
        error "Not a valid DM20 play directory: ${play_dir}"
        exit 1
    fi

    # ── Platform info ─────────────────────────────────────────────────────
    echo ""
    if [[ "$PLATFORM" == "macos-arm" ]]; then
        echo -e "  ${GREEN}${BOLD}Apple Silicon detected${NC} — Full local TTS stack:"
        echo -e "    ${BOLD}Tier 1${NC} (speed):   Kokoro 82M — local, <300ms"
        echo -e "    ${BOLD}Tier 2${NC} (quality):  mlx-audio  — local, ~800ms"
        echo -e "    ${BOLD}Tier 3${NC} (fallback): Edge-TTS   — cloud, free"
    elif [[ "$PLATFORM" == "macos-intel" ]]; then
        echo -e "  ${YELLOW}${BOLD}Intel Mac detected${NC} — Cloud TTS fallback:"
        echo -e "    ${BOLD}Tier 3${NC} (fallback): Edge-TTS   — cloud, free"
        echo -e "  ${DIM}Local models (Kokoro, mlx-audio) require Apple Silicon.${NC}"
    else
        echo -e "  ${BOLD}Platform:${NC} ${PLATFORM}"
        echo -e "    ${BOLD}Tier 3${NC} (fallback): Edge-TTS   — cloud, free"
    fi
    echo ""

    # ── Install voice extras ──────────────────────────────────────────────
    step "Installing voice dependencies..."

    local installed=false
    if uv tool install "dm20-protocol[voice] @ git+${REPO_URL}" --force 2>&1; then
        success "Voice dependencies installed"
        installed=true
    else
        warn "Install failed with default Python. Retrying with Python 3.12..."
        if uv tool install --python 3.12 "dm20-protocol[voice] @ git+${REPO_URL}" --force 2>&1; then
            success "Voice dependencies installed (using Python 3.12)"
            installed=true
        else
            error "Voice installation failed. Check the output above."
            exit 1
        fi
    fi

    # ── Summary ───────────────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}${GREEN}✅ Voice installed!${NC}"
    echo ""
    echo -e "  ${BOLD}Enable narrated mode:${NC}"
    echo -e "    Open Claude Code in ${play_dir}"
    echo -e "    Run ${BOLD}/dm:profile${NC} → set mode to ${BOLD}Narrated${NC} or ${BOLD}Immersive${NC}"
    echo ""
    echo -e "  ${DIM}Narrated: DM speaks the narrative aloud after each action${NC}"
    echo -e "  ${DIM}Immersive: Narrated + players can speak their actions${NC}"
    echo ""
}

# ─── Main ──────────────────────────────────────────────────────────────────────

main() {
    # --plugin may appear anywhere; strip it before positional parsing.
    local args=()
    for arg in "$@"; do
        if [[ "$arg" == "--plugin" ]]; then
            PLUGIN_MODE=true
        else
            args+=("$arg")
        fi
    done
    set -- "${args[@]+"${args[@]}"}"

    # Parse --upgrade flag before anything else
    if [[ "${1:-}" == "--upgrade" ]]; then
        UPGRADE_MODE=true
        banner
        detect_platform
        do_upgrade "${2:-}"
        exit 0
    fi

    # Parse --voice flag
    if [[ "${1:-}" == "--voice" ]]; then
        VOICE_MODE=true
        banner
        detect_platform
        do_install_voice "${2:-}"
        exit 0
    fi

    banner
    detect_platform
    detect_mode
    check_prerequisites
    gather_options

    if [[ "$PLUGIN_MODE" == true && "$INSTALL_MODE" == "developer" ]]; then
        warn "--plugin applies to user-mode play directories; a developer clone already loads .claude/ directly."
        PLUGIN_MODE=false
    fi

    if [[ "$INSTALL_MODE" == "user" ]]; then
        do_tool_install
        do_create_play_dir
        detect_icloud
        do_configure_mcp
        do_verify
    else
        do_clone
        detect_icloud
        do_install_deps
        do_create_data_dirs
        do_write_env
        do_configure_mcp
        do_verify
    fi

    print_summary
}

main "$@"
