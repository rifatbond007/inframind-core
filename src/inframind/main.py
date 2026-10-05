"""InfraMind entry point.

Step 0 placeholder. Real services are added in their owning phases.
"""

from __future__ import annotations

import sys

from . import __version__


def main() -> int:
    """Print the version and exit. Replaced by service entrypoints as we land phases."""
    print(f"inframind {__version__} (setup scaffold)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
