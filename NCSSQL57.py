"""Compatibility shim for the legacy ``NCSSQL57`` module.

The implementation has moved into the ``scheduler`` package. This file
re-exports legacy symbols so existing imports continue to work while
call sites migrate to focused modules.
"""

from scheduler.domain import *
from scheduler.repositories import *
from scheduler.engine import *
from scheduler.profiling import *
from scheduler.debug import *
from scheduler.ui_legacy import *

# Re-export all remaining legacy symbols for compatibility.
from scheduler.legacy_core import *
