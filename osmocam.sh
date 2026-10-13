#!/bin/sh
# Starts OsmoCam on macOS or Linux. The first run creates a private Python environment and installs
# the requirements (needs internet once). Arguments pass through to osmocam.py, e.g.  ./osmocam.sh --no-vcam
set -e
here=$(cd "$(dirname "$0")" && pwd)
case "$(uname -s)" in
    Darwin) data="$HOME/Library/Application Support/osmo-pc" ;;
    *) data="${XDG_DATA_HOME:-$HOME/.local/share}/osmo-pc" ;;
esac
venv="$data/venv-cam"
python="$venv/bin/python"
requirements="$here/requirements.txt"
stamp="$venv/requirements.sha256"
# Python versions with ready-made builds of every library used here, newest first. Python 3.15 (October
# 2026) still lacks some of them (pip then tries to compile them and fails), so it is not used.
versions="3.14 3.13 3.12 3.11 3.10"

# An install that never finished: start over (and choose the Python again).
if [ -d "$venv" ] && [ ! -f "$stamp" ]; then
    rm -rf "$venv"
fi
if [ ! -x "$python" ]; then
    echo "First run: creating the Python environment..."
    base=""
    for candidate in $(for v in $versions; do printf 'python%s ' "$v"; done) python3; do
        version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
        case " $versions " in
            *" $version "*) base="$candidate"; break ;;
        esac
    done
    if [ -z "$base" ]; then
        echo
        echo "Python 3.10 to 3.14 is needed (3.14 recommended). Python 3.15 is too new for now: some of the"
        echo "libraries this app uses have no build for it yet. Install Python 3.14 (it can stay next to other"
        echo "versions) from https://www.python.org/downloads/ (on macOS, the \"macOS 64-bit universal2"
        echo "installer\"; with Homebrew: brew install python@3.14), then run this again."
        exit 1
    fi
    mkdir -p "$data"
    if ! "$base" -m venv "$venv"; then
        rm -rf "$venv"
        echo "Could not create the Python environment (on Debian or Ubuntu: sudo apt install python3-venv)"
        exit 1
    fi
fi
sum=$( (sha256sum "$requirements" 2>/dev/null || shasum -a 256 "$requirements") | cut -d ' ' -f 1)
if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$sum" ]; then
    echo "Installing the requirements..."
    if ! "$python" -m pip install --disable-pip-version-check -q --only-binary=:all: -r "$requirements"; then
        version=$("$python" -c 'import sys, platform; print("%d.%d, %s" % (sys.version_info[0], sys.version_info[1], platform.machine()))')
        rm -rf "$venv"
        echo "Installing the requirements failed (Python $version). Check the internet connection and the"
        echo "message above, then run this again."
        exit 1
    fi
    echo "$sum" > "$stamp"
fi
exec "$python" "$here/osmocam.py" "$@"
