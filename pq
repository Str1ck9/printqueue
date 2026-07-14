#!/usr/bin/env python3
"""PrintQueue launcher — runs the CLI from a source checkout.

Prefers the project's .venv interpreter (where bambu-lab-cloud-api lives)
when it exists and we're not already running inside it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

VENV_PY = os.path.join(HERE, ".venv", "bin", "python3")
if os.path.exists(VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
    os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])

# Ensure we can import the local package regardless of CWD
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from printqueue.cli import main

if __name__ == "__main__":
    sys.exit(main())
