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

# An install that never finished: start over (and choose the Python again).
if [ -d "$venv" ] && [ ! -f "$stamp" ]; then
    rm -rf "$venv"
fi
if [ ! -x "$python" ]; then
    echo "First run: creating the Python environment..."
    # Newest first, but Python 3.15 (October 2026) still lacks builds of some of the libraries: it is
    # only used when nothing from 3.10 to 3.14 is installed.
    base=""
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            base="$candidate"
            break
        fi
    done
    if [ -z "$base" ]; then
        echo "Python 3.10 or newer is required (3.14 recommended): https://www.python.org/downloads/"
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
    if ! "$python" -m pip install --disable-pip-version-check -q -r "$requirements"; then
        version=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        too_new=$("$python" -c 'import sys; print(int(sys.version_info[:2] > (3, 14)))')
        rm -rf "$venv"
        echo "Installing the requirements failed (Python $version). Check the internet connection."
        if [ "$too_new" = 1 ]; then
            echo "Python $version is too new for some of the libraries: install Python 3.14 from"
            echo "https://www.python.org/downloads/ and run this again."
        fi
        exit 1
    fi
    echo "$sum" > "$stamp"
fi
exec "$python" "$here/osmocam.py" "$@"
