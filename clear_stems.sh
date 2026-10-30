#!/bin/sh
# Linux / Mac: free disk space by clearing split instruments from your songs.
cd "$(dirname "$0")" && exec python3 clear_stems.py "$@"
