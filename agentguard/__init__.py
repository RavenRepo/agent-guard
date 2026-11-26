"""agent-guard: stop coding agents from avoiding the work they were asked to do."""

VERSION = "0.3.0"


def self_command() -> list[str]:
    """How to run this same copy of agent-guard in a child process: the checkout's or installed tree's entry point,
    or the module when installed as a package."""
    import os
    import sys
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin", "agent-guard")
    return [sys.executable, "-B", entry] if os.path.isfile(entry) else [sys.executable, "-B", "-m", "agentguard"]
