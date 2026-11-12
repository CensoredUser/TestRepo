#!/usr/bin/env python3
"""Entry point used by the claude.ai skill: python3 <skill dir>/run.py <command> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True   # the skill folder may be read-only

from bambu3mf.cli import main  # noqa: E402

sys.exit(main())
