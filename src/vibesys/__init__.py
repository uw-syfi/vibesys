"""VibeSys core and orchestration runtime.

This package's ``__init__.py`` is intentionally empty so that submodules
used as subprocess entrypoints do not drag in heavy optional dependencies via
package-level re-exports.

Import what you need by full module path, e.g.::

    from vs_agent.api import build_agent_client
    from vibesys.api import create_session
"""
