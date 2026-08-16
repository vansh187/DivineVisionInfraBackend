"""
Shared setup for one-off scripts in this directory: puts the repo root on sys.path (so
`from DivineService...` etc. resolve when run as `python scripts/whatever.py`) and loads
.env, in that order. Call setup() before any repo-internal import.

Usage:
    from _bootstrap import setup
    setup()
"""
import sys
import os


def setup() -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dotenv import load_dotenv
    load_dotenv()
