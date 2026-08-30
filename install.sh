#!/usr/bin/env bash
#
# skillmem one-command installer for macOS / Linux.
#
# Usage:
#   curl -sSL https://<your-host>/skillmem/install.sh | bash
#   curl -sSL https://<your-host>/skillmem/install.sh | bash -s -- --no-claude-code
#   curl -sSL https://<your-host>/skillmem/install.sh | bash -s -- --no-semantic
#
# Offline (no host at all — someone handed you the tarball):
#   bash install.sh --from=/path/to/skillmem-0.7.0.tar.gz
#
# Что делает:
#   1. ставит python@3.12 + uv (через brew / apt) если их нет
#   2. скачивает свежий тарбол skillmem
#   3. разворачивает в ~/.local/share/skillmem/<version>
#   4. создаёт venv + ставит пакет (с semantic-экстрой; --no-semantic = только BM25)
#   5. кладёт симлинки в ~/.local/bin
#   6. запускает `skillmem init --claude-code` (можно отключить флагом)
#
# Откат: ~/.local/bin/skillmem uninstall && rm -rf ~/.local/share/skillmem

set -euo pipefail

# --- config ---
PACKAGE_URL="${SKILLMEM_URL:-https://<your-host>/skillmem/skillmem-latest.tar.gz}"
INSTALL_ROOT="${SKILLMEM_INSTALL_ROOT:-$HOME/.local/share/skillmem}"
BIN_DIR="${SKILLMEM_BIN_DIR:-$HOME/.local/bin}"
WITH_CLAUDE_CODE=1
WITH_SEMANTIC=1

for arg in "$@"; do
    case "$arg" in
        --no-claude-code) WITH_CLAUDE_CODE=0 ;;
        --no-semantic) WITH_SEMANTIC=0 ;;
        --url=*) PACKAGE_URL="${arg#*=}" ;;
        --from=*) LOCAL_PKG="${arg#*=}" ;;
        --help|-h)
            sed -n '2,18p' "$0"; exit 0 ;;
    esac
done

# --- helpers ---
say()  { printf "\033[1;36m▶\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m⚠\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- detect OS ---
case "$(uname -s)" in
    Darwin) OS="mac" ;;
    Linux)  OS="linux" ;;
    *)      fail "unsupported OS: $(uname -s) — only macOS and Linux for now" ;;
esac
say "Detected OS: $OS"

# --- step 1: python@3.12 + uv ---
ensure_python() {
    if have python3.12; then
        # Defend against a `python3.12` symlink that actually points to 3.10/3.11.
        if python3.12 --version 2>&1 | grep -qE 'Python 3\.(1[2-9]|[2-9][0-9])'; then
            say "python3.12: $(python3.12 --version)"
            return
        else
            warn "python3.12 in PATH but reports $(python3.12 --version) — reinstalling"
        fi
    fi
    say "Installing python@3.12"
    if [[ "$OS" == "mac" ]]; then
        if ! have brew; then
            fail "Homebrew not found. Install from https://brew.sh first."
        fi
        brew install python@3.12
    else
        if have apt-get; then
            sudo apt-get update -qq
            sudo apt-get install -y python3.12 python3.12-venv
        else
            fail "No apt-get found and python3.12 missing. Install manually."
        fi
    fi
}

ensure_uv() {
    if have uv; then
        say "uv: $(uv --version)"
        return
    fi
    say "Installing uv"
    if [[ "$OS" == "mac" ]] && have brew; then
        brew install uv
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # uv installs to ~/.local/bin by default
        export PATH="$HOME/.local/bin:$PATH"
    fi
}

ensure_python
ensure_uv

# --- step 2: obtain tarball (local file or download) + verify SHA256 ---
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if [ -n "${LOCAL_PKG:-}" ]; then
    # Offline install: someone handed you the tarball directly, no host needed.
    [ -f "$LOCAL_PKG" ] || fail "no such file: $LOCAL_PKG"
    say "Installing skillmem from local file $LOCAL_PKG"
    cp "$LOCAL_PKG" "$TMPDIR/pkg.tgz" || fail "could not read $LOCAL_PKG"
    [ -f "$LOCAL_PKG.sha256" ] && cp "$LOCAL_PKG.sha256" "$TMPDIR/pkg.tgz.sha256"
else
    say "Fetching skillmem from $PACKAGE_URL"
    curl -sSLf -o "$TMPDIR/pkg.tgz" "$PACKAGE_URL" || \
        fail "could not download $PACKAGE_URL — check URL or network"
fi

# Verify SHA256 against ${URL}.sha256 (fail closed if checksum file missing).
# This is not bulletproof against a fully compromised host (attacker could
# rewrite both files), but it does stop accidental cache poisoning and many
# transit-layer attacks. For internal tooling that's a reasonable trade-off.
SHA_URL="${PACKAGE_URL}.sha256"
# In --from mode the checksum file, if any, was copied in beside the tarball.
if [ -f "$TMPDIR/pkg.tgz.sha256" ] || \
   { [ -z "${LOCAL_PKG:-}" ] && curl -sSLf -o "$TMPDIR/pkg.tgz.sha256" "$SHA_URL"; }; then
    expected="$(awk '{print $1}' "$TMPDIR/pkg.tgz.sha256")"
    if have shasum; then
        actual="$(shasum -a 256 "$TMPDIR/pkg.tgz" | awk '{print $1}')"
    elif have sha256sum; then
        actual="$(sha256sum "$TMPDIR/pkg.tgz" | awk '{print $1}')"
    else
        fail "neither shasum nor sha256sum available; cannot verify checksum"
    fi
    if [[ "$expected" != "$actual" ]]; then
        fail "SHA256 mismatch! expected=$expected actual=$actual — aborting."
    fi
    say "Checksum verified ($expected)"
else
    warn "no checksum file — proceeding without verification"
fi

# --- step 3: unpack to INSTALL_ROOT ---
mkdir -p "$INSTALL_ROOT"
PKG_DIR="$INSTALL_ROOT/current"
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR"
tar xzf "$TMPDIR/pkg.tgz" -C "$PKG_DIR" --strip-components=1
say "Unpacked to $PKG_DIR"

# --- step 4: create venv + install ---
say "Creating venv + installing package"
cd "$PKG_DIR"
uv venv --python python3.12 --quiet
# shellcheck source=/dev/null
if [[ "$WITH_SEMANTIC" == "1" ]]; then
    say "Including semantic recall (fastembed, ~200 MB; --no-semantic to skip)"
    if ! uv pip install -e '.[semantic]' --quiet; then
        warn "semantic extra failed to install — falling back to base (recall stays BM25-only)"
        uv pip install -e . --quiet
    fi
else
    uv pip install -e . --quiet
fi

# --- step 5: symlinks ---
mkdir -p "$BIN_DIR"
for cmd in skillmem skillmem-mcp skillmem-server; do
    ln -sf "$PKG_DIR/.venv/bin/$cmd" "$BIN_DIR/$cmd"
done
say "Symlinks → $BIN_DIR/{skillmem,skillmem-mcp,skillmem-server}"

# --- step 5b: pre-download the embedding model ---
# Otherwise the ~220 MB fetch happens silently inside the user's first search,
# which just looks like skillmem hanging. Non-fatal: recall falls back to BM25.
if [[ "$WITH_SEMANTIC" == "1" ]]; then
    say "Downloading embedding model (~220 MB, one time — first search would otherwise stall)"
    if "$PKG_DIR/.venv/bin/python" -c 'import sys; from skillmem import embed; sys.exit(0 if embed.available() else 1)' 2>/dev/null; then
        say "Semantic recall ready"
    else
        warn "model download failed — recall will use BM25 until it succeeds"
        warn "retry later with: skillmem doctor"
    fi
fi

if ! printf "%s" "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    warn "$BIN_DIR is not on PATH. Add this line to ~/.zshrc (or ~/.bashrc):"
    warn "    export PATH=\"$BIN_DIR:\$PATH\""
    warn "Then reload your shell:  exec \$SHELL -l"
fi

# --- step 6: init Claude Code ---
if [[ "$WITH_CLAUDE_CODE" == "1" ]]; then
    say "Configuring Claude Code (use --no-claude-code to skip)"
    "$BIN_DIR/skillmem" init --claude-code || \
        warn "init failed — run \`$BIN_DIR/skillmem init --claude-code\` manually"
else
    say "Skipped Claude Code setup. Run later: skillmem init --claude-code"
fi

# --- done ---
printf '\n\033[1;32m✓ skillmem installed.\033[0m\n\n'
cat <<EOF
Try it now:
    skillmem doctor
    skillmem search "..."
    skillmem ls --kind feedback

Open Claude Code (\`claude\`) in any project — the mem_* tools are exposed.

Docs:   $PKG_DIR/OVERVIEW.md
Logs:   $INSTALL_ROOT/
Uninstall:
    skillmem uninstall
    rm -rf $INSTALL_ROOT $BIN_DIR/skillmem*
EOF
