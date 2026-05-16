"""ALITA -- Autonomous Listener, Investigator, Thinker, Aide.

The Pelops agent and its supporting modules live here. We intentionally
do NOT eagerly re-export `build_agent` at this top level, because doing
so triggers `pelops.tools` -> `vstash` at import time -- breaking any
environment (e.g. CI) that doesn't have the local `vstash-local` install.

Import the concrete module you need:

    from pelops.agent import build_agent
    from pelops import jobs, watchers, metrics
"""

__version__ = "0.1.0"
