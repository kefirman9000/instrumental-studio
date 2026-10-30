#!/bin/sh
# Linux / Mac: check everything Instrumental Studio needs and install what's missing.
cd "$(dirname "$0")" && exec python3 check_setup.py "$@"
