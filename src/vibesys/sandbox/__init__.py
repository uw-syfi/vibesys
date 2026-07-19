"""Execution policy: where and how the workspace runs.

Modules:
  - ``run_environment``    — top-level RunEnvironmentSpec / RunEnvironment;
                              bridges CLI args to a sandbox factory.

The sandbox backends themselves (``DockerSandbox``, ``ModalSandbox``, and
Modal Volume weight staging) live in the ``vs_sandbox`` package under
``libs/vs-sandbox``.
"""
