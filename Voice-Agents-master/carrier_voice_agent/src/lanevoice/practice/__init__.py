"""Practice mode — a rep pitches a simulated customer, and the system grades the rep.

The customer is an LLM playing one of the shipped mood profiles (`profiles.py`),
driven from the dashboard the same way the playground drives the sales agent.
"""

from lanevoice.practice.profiles import CustomerProfile, load_profiles
from lanevoice.practice.sessions import PracticeSessionManager

__all__ = ["CustomerProfile", "PracticeSessionManager", "load_profiles"]
