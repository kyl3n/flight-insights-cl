"""`python -m flight_insights`, and the entry point of the frozen engine."""

import sys

from flight_insights.cli import main

if __name__ == "__main__":
    sys.exit(main())
