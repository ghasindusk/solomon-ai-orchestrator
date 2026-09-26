"""Octavryn SI -- Symbiotic Intelligence Orchestration System (v0.6 alpha).

Octavryn SI was originally released as Solomon AI Orchestrator v0.4.0-alpha.

During v0.6 Phase 1A the implementation still lives in the `solomon` package
so the compatibility migration remains reviewable. `octavryn` is the
canonical import and CLI name: `import octavryn.router` returns the very
same module object as `import solomon.router`, so there is one set of
module globals, not two diverging copies.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

__version__ = "0.6.0a1"
PRODUCT_NAME = "Octavryn SI"
HISTORICAL_NOTE = "Octavryn SI was originally released as Solomon AI Orchestrator v0.4.0-alpha."

# Modules that physically exist in this package and must not be aliased.
_OWN = {"octavryn.cli", "octavryn.__main__", "octavryn.mcp_server"}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str):
        self.target = target

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        self._orig = (module.__spec__, getattr(module, "__loader__", None))
        return module

    def exec_module(self, module):
        # Already executed under its solomon name. The import machinery has
        # just overwritten __spec__/__loader__ with the alias spec; put the
        # originals back so the shared module keeps one consistent identity.
        module.__spec__, module.__loader__ = self._orig


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("octavryn.") or fullname in _OWN:
            return None
        real = "solomon." + fullname[len("octavryn."):]
        if importlib.util.find_spec(real) is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
