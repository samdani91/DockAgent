"""
coordination — Module 4: agentic coordination.

Drives Modules 1-3 and closes the unified feedback loop: a container-test
failure routes back to Dockerfile repair, and a flakiness repair routes forward
for re-verification.

Routing is deterministic. The LLM is used only to explain outcomes and answer
developer questions — see explain.py.
"""

__version__ = "0.1.0"
