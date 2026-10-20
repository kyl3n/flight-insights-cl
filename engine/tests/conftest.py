import os

from hypothesis import settings

# "ci" keeps the suite quick; HYPOTHESIS_PROFILE=deep runs a long fuzzing session.
settings.register_profile("ci", max_examples=150)
settings.register_profile("deep", max_examples=5000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
