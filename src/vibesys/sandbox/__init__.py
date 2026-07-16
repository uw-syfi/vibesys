"""Execution policy: where and how the workspace runs.

Modules:
  - ``run_environment``    — top-level RunEnvironmentSpec / RunEnvironment;
                              bridges CLI args to a sandbox factory.
  - ``docker_sandbox``     — ``DockerSandbox`` (BaseSandbox subclass).
  - ``modal_sandbox``      — ``ModalSandbox`` (BaseSandbox subclass).
  - ``modal_model_setup``  — Modal Volume + weight staging for Modal runs.
"""
