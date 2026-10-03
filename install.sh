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

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) say "This installer supports macOS and Linux. On Windows, follow the README."; exit 1 ;;
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
else
    source_spec="git+$REPO_URL"
fi

if ! command -v uv >/dev/null 2>&1; then
    step "Installing uv, the Python tool manager unimem uses"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The uv installer puts uv in ~/.local/bin; make it usable in this run.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi

if [ -z "$want_model" ]; then
    want_model=no
    if [ "$apple_silicon" = yes ] && [ -t 0 ] && [ -t 1 ]; then
        say ""
        say "unimem can use a small local model to make recall smarter."
        say "It runs only when you ask it to, never during normal recall,"
        say "and needs Apple Silicon plus $MODEL_SIZE of disk."
        printf 'Install the local model too? [y/N] '
        read -r answer || answer=""
        case "$answer" in y|Y|yes|YES) want_model=yes ;; esac
    fi
fi

if [ "$want_model" = yes ] && [ "$apple_silicon" = no ]; then
    say "The local model needs a Mac with Apple Silicon; installing unimem without it."
    want_model=no
fi

package="unimem"
if [ "$want_model" = yes ]; then
    package="unimem[enrich]"
fi

step "Installing unimem"
uv tool install --force --python 3.12 "$package @ $source_spec"
uv tool update-shell >/dev/null 2>&1 || true

bin_dir=$(uv tool dir --bin)
unimem="$bin_dir/unimem"

if [ "$want_model" = yes ]; then
    step "Downloading the local model ($MODEL_SIZE, one time)"
    "$unimem" enrich --download
fi

step "Checking the installation"
"$unimem" --version

say ""
say "unimem is installed."
say ""
say "Next, open a terminal in a project folder and connect your coding tools:"
say "    unimem init --client all"
    say ""
    say "Or connect one tool at a time; the README lists a command for each."
if [ "$want_model" = yes ]; then
    say ""
    say "To improve recall with the local model later, run:"
    say "    unimem enrich"
fi
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say ""
       say "Open a new terminal window first so the 'unimem' command is found." ;;
esac
