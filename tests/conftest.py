import os

from hypothesis import settings

# Deterministic by default (principles: same inputs, same outputs). Set
# HYPOTHESIS_PROFILE=explore to search with fresh random examples and more of them.
settings.register_profile("default", derandomize=True)
settings.register_profile("explore", max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
