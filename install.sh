#!/bin/sh
# Install unimem, and optionally its local enrichment model, on macOS or Linux.
#
#   sh install.sh               install unimem; asks about the model on Apple Silicon
#   sh install.sh --with-model  also install and download the local model (Apple Silicon)
#   sh install.sh --no-model    never install the model
#
# Run it from a downloaded copy of the repository, or pipe it from GitHub.
# It installs uv (https://docs.astral.sh/uv/) when missing, then installs unimem
# as an isolated command-line tool. Nothing else on the system is changed.
set -eu

REPO_URL="https://github.com/Yabuku-xD/unimem"
MODEL_SIZE="about 660 MB"
want_model=""

for arg in "$@"; do
    case "$arg" in
        --with-model) want_model=yes ;;
        --no-model) want_model=no ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# Colour only when writing to a terminal that wants it.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    bold=$(printf '\033[1m'); dim=$(printf '\033[2m'); green=$(printf '\033[32m')
    red=$(printf '\033[31m'); reset=$(printf '\033[0m'); live=yes
else
    bold=""; dim=""; green=""; red=""; reset=""; live=no
fi

say() { printf '%s\n' "$*"; }
ok() { printf '  %s✓%s %s\n' "$green" "$reset" "$*"; }
fail() { printf '  %s✗%s %s\n' "$red" "$reset" "$*" >&2; }

log=$(mktemp "${TMPDIR:-/tmp}/unimem-install.XXXXXX")
trap 'rm -f "$log"' EXIT

# run_step "doing" "done" command...
# Runs the command quietly and reports one line. Its output is shown only if it fails.
run_step() {
    doing=$1; finished=$2; shift 2
    if [ "$live" = yes ]; then
        printf '  %s…%s %s' "$dim" "$reset" "$doing"
    fi
    if "$@" >"$log" 2>&1; then
        [ "$live" = yes ] && printf '\r\033[K'
        ok "$finished"
    else
        [ "$live" = yes ] && printf '\r\033[K'
        fail "$doing failed"
        say ""
        sed 's/^/    /' "$log" >&2
        exit 1
    fi
}

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) say "unimem currently supports macOS and Linux."; exit 1 ;;
esac

apple_silicon=no
if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
    apple_silicon=yes
fi

# Install from this checkout when the script sits in one; otherwise from GitHub.
script_dir=$(CDPATH="" cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$script_dir" ] && [ -f "$script_dir/pyproject.toml" ] \
    && grep -q '^name = "unimem"' "$script_dir/pyproject.toml"; then
    source_spec="file://$script_dir"
    source_label="this folder"
else
    source_spec="git+$REPO_URL"
    source_label="GitHub"
fi

say ""
say "${bold}unimem installer${reset}"
say "${dim}One local memory for all your coding agents.${reset}"
say ""

if [ -z "$want_model" ]; then
    want_model=no
    if [ "$apple_silicon" = yes ] && [ -t 0 ] && [ -t 1 ]; then
        say "unimem can use a small local model to make recall smarter."
        say "It runs only when you ask it to, never during normal recall,"
        say "and needs $MODEL_SIZE of disk."
        printf 'Install the local model too? [y/N] '
        read -r answer || answer=""
        case "$answer" in y|Y|yes|YES) want_model=yes ;; esac
        say ""
    fi
fi

if [ "$want_model" = yes ] && [ "$apple_silicon" = no ]; then
    say "${dim}The local model needs a Mac with Apple Silicon; installing without it.${reset}"
    want_model=no
fi

install_uv() { curl -LsSf https://astral.sh/uv/install.sh | sh; }

if command -v uv >/dev/null 2>&1; then
    ok "uv is already installed"
else
    run_step "Installing uv, the tool manager unimem runs on" "uv installed" install_uv
    # The uv installer puts uv in ~/.local/bin; make it usable in this run.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi

package="unimem"
if [ "$want_model" = yes ]; then
    package="unimem[enrich]"
fi

run_step "Installing unimem from $source_label" "unimem installed from $source_label" \
    uv tool install --force --python 3.12 "$package @ $source_spec"
uv tool update-shell >/dev/null 2>&1 || true

bin_dir=$(uv tool dir --bin)
unimem="$bin_dir/unimem"

if [ "$want_model" = yes ]; then
    run_step "Downloading the local model ($MODEL_SIZE, one time)" "Local model downloaded" \
        "$unimem" enrich --download
fi

version=$("$unimem" --version 2>/dev/null) || { fail "unimem did not start"; exit 1; }
ok "$version is ready"

say ""
say "${bold}Next step${reset}"
say "  unimem init        ${dim}connect your coding tools, once, from any folder${reset}"
if [ "$want_model" = yes ]; then
    say "  unimem enrich      ${dim}improve recall with the local model, whenever you like${reset}"
fi
say ""
say "${dim}One tool at a time: unimem init --client claude (or codex, cursor, pi, hermes).${reset}"
say "${dim}To remove everything later: unimem uninstall${reset}"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say ""
       say "Open a new terminal window first so the 'unimem' command is found." ;;
esac
say ""
