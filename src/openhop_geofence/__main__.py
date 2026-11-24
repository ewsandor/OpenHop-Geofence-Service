"""Allow ``python -m openhop_geofence``."""

import sys

from .cli import main

sys.exit(main())
