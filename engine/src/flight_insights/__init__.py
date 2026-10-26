"""Flight Insights analysis engine."""

import os
from importlib.metadata import version

# One BLAS thread. The engine spreads its own work across cores, and OpenBLAS's threads
# on top of that oversubscribe a busy laptop: with every core busy, a tune-check flight's
# analysis took 52 s with OpenBLAS's threads and 5 s with one. It must be set before NumPy
# loads its BLAS; the environment may still choose otherwise.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = version("flight-insights-engine")
