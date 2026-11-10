#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/scripts/install_linux.py" "$@"
