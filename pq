#!/usr/bin/env python3
"""PrintQueue launcher — runs the CLI from a source checkout.

Re-execs into the project's .venv interpreter (where bambu-lab-cloud-api
lives) unless we're already running inside it. NOTE: compare sys.prefix,
not executable paths — the venv's python3 is a symlink to the system
binary, so realpath(sys.executable) can't tell the two apart.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
VENV_PY = os.path.join(VENV, "bin", "python3")

in_venv = os.path.abspath(sys.prefix) == os.path.abspath(VENV)
if os.path.exists(VENV_PY) and not in_venv:
    os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])

# Ensure we can import the local package regardless of CWD
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from printqueue.cli import main

if __name__ == "__main__":
    sys.exit(main())
