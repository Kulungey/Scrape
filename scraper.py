#!/usr/bin/env python3
"""Backward-compat shim.

The implementation moved to the `scrape/` package (see README.md for the
new module layout). This file just forwards to it so `python scraper.py URL`
keeps working exactly as before.
"""
import sys

from scrape.cli import main

if __name__ == "__main__":
    sys.exit(main())
