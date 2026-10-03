"""PyInstaller entry point (absolute import so the package resolves in a frozen app)."""
import multiprocessing
import sys

from cueforge.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
