"""Gate for the slow part of the kit's own tests (rule W12): set KIT_TESTS_SLOW=1 to run it."""
import os
import unittest

SLOW_ENV = "KIT_TESTS_SLOW"


def skip_unless_slow():
    """Decorator for a TestCase class or method that builds whole repositories or spawns many processes."""
    return unittest.skipUnless(os.environ.get(SLOW_ENV) == "1", f"slow case: set {SLOW_ENV}=1 to run it")
