"""Frontend-serving components for VibeSys applications.

The package intentionally has no eager re-exports. Composition roots import
the specific components they need, and importing a protocol or event model
does not initialize the server runtime.

``lazy_module`` is defined here rather than in a module of its own for a count. The host helper
answers ``hello`` within a backstop on the modules it loads (``tests/e2e/test_host_helper.py``) and
imports this package anyway, so the primitive costs it no module, while a module of its own
would cost it one.

Public surface: ``lazy_module``.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

__all__ = ["lazy_module"]


class _LazyModule(ModuleType):
    """The handle: it names a module and imports it on the first attribute it does not have."""

    def __getattr__(self, name: str) -> object:
        # A dunder asked of the handle (``__wrapped__``, ``__path__``: a tool that inspects
        # module globals) is about the handle, so it must not import the module.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(import_module(self.__name__), name)


def lazy_module(name: str) -> ModuleType:
    """A module imported when it is first used, declared beside the import that types it.

    ``vibesys host <read>`` is a new process for every read, so every module a read imports at
    load is paid by every read. A module that only some paths use is declared lazy::

        if TYPE_CHECKING:
            from server import host_examples
        else:
            host_examples = lazy_module("server.host_examples")

    The first branch is an ordinary import to everything that reads the source: the type checker
    types ``host_examples.ProjectLookupError``, and tach sees the edge
    (``ignore_type_checking_imports`` is off in ``tach.toml``). The process runs the second
    branch, whose handle imports the module the first time an attribute is read, so an
    ``except host_examples.ProjectLookupError`` loads the module only once an error reaches it.
    ``test_every_lazy_module_in_product_code_is_declared_by_an_import`` holds the two branches to
    the same module and name.

    Declare a module lazy only where it is not loaded anyway. A process that builds a thing at
    start (the host helper's ``real_host``) imports its module statically. A handle loads through
    ``importlib``, which ``-X importtime`` does not report, so the tests that hold a process to
    the modules it may load read ``sys.modules`` in that process.
    """
    return _LazyModule(name)
