"""Services over HTTP: the workload, its operations and the requests they build.

A ``Workload`` is a model and a weighted mix of ``Operation``s. Each
operation's ``build(model, rand)`` returns a ``Request`` of one or more
``Step``s (``get``, ``post``, ``put``); a later step can send a value an
earlier one captured (``ref``, a ``Template``).
"""

from vibesys_evaluator._private.http import Request, Step, Template, get, post, put, ref
from vibesys_evaluator._private.workload import Operation, Workload

__all__ = ["Operation", "Request", "Step", "Template", "Workload", "get", "post", "put", "ref"]
