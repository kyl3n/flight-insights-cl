import os

# The engine's own setting (flight_insights/__init__.py), before any test module loads NumPy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from hypothesis import settings

# "ci" keeps the suite quick; HYPOTHESIS_PROFILE=deep runs a long fuzzing session.
settings.register_profile("ci", max_examples=150)
settings.register_profile("deep", max_examples=5000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
