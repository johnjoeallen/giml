"""Machine-learning layers (spec section 16): optimisation and triage on top of the deterministic engine.

Every layer here is judged against the deterministic baseline before anything reads its output, and with
every layer off the planner gives the same verified result, only with more builds and no triage (hard rule 8).
No layer calls a hosted or frontier model, at training or at inference; installing this package's ``ml`` extra
pulls in scikit-learn, which runs entirely on the local CPU.
"""
