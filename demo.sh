#!/usr/bin/env bash
# One-command demonstration of the rewind harness.  Exit 0 means all three
# scenarios matched their stated expectations.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m rewind demo --rounds 20
