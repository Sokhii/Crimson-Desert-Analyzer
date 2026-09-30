"""PyInstaller / source launcher for Crimson Soundtrack Studio."""

import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cstudio.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
