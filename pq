#!/usr/bin/env python3
"""PrintQueue launcher — runs the CLI from a source checkout."""
import os
import sys

# Ensure we can import the local package regardless of CWD
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from printqueue.cli import main

if __name__ == "__main__":
    sys.exit(main())
