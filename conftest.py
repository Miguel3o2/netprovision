"""Pytest bootstrap: make the project root importable from any test module."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
