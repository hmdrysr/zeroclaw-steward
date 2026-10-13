"""Linux <-> plain English, deterministic and fail-safe.

explain.py   what a command does, and how risky it is, before it runs
outputs.py   command output -> short English facts
errors.py    error messages and exit codes -> what went wrong and what to do
intents.py   plain-English requests -> known, tested Steward plans

Rule everywhere: if something is not in the curated knowledge, the answer is
"I don't know", never a guess.
"""
