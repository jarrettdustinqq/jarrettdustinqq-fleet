#!/usr/bin/env python3
"""Compatibility import for the hardened JACS snapshot boundary.

The executable implementation lives in :mod:`jacs_snapshot_boundary`. Keeping
this module as a re-export prevents older Fleet imports from bypassing the
versioned snapshot-envelope, replay, source-authority, dependency, and freshness
gates.
"""

from jacs_snapshot_boundary import *  # noqa: F401,F403
