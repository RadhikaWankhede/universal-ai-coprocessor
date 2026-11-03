"""PyInstaller entry point. Equivalent to `py -m uaic.desktop`."""
from uaic.desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
