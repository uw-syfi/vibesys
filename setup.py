"""Setuptools shim that stages release-owned assets into the wheel.

Project metadata lives in ``pyproject.toml``; this file exists only to hook a
custom ``build_py`` step that copies a prebuilt, target-specific TUI payload,
framework resources, and the input SDK. Setuptools never installs JavaScript
dependencies or mutates the checkout. Development builds without a payload
remain headless; release builds require every staged input.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools.command.build_py import build_py as _build_py
from setuptools.dist import Distribution as _Distribution

_REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(_REPO_ROOT))
# Build helpers live in packaging/ as top-level modules. There is deliberately no
# packaging/__init__.py: a `packaging` package would shadow the PyPA library.
sys.path.insert(0, str(_REPO_ROOT / "packaging"))
# lint-waiver: LW-008020 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from packaging_support import (  # noqa: E402
    clear_distribution_build_outputs,
    discover_distribution_packages,
    release_has_native_payload,
)

# lint-waiver: LW-008021 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from resources_packaging import stage_resources, stage_sdk  # noqa: E402

# lint-waiver: LW-008022 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from tui_packaging import stage_prebuilt_tui  # noqa: E402

# lint-waiver: LW-008023 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from wheel_targets import WheelTarget, resolve_wheel_target  # noqa: E402


class BuildPy(_build_py):
    """Standard ``build_py`` plus staging the compiled TUI into the package."""

    def run(self) -> None:
        """Stage build outputs after setuptools copies Python packages."""
        clear_distribution_build_outputs(Path(self.build_lib), self.packages or [])
        super().run()
        vibesys_package_root = Path(self.build_lib) / "vibesys"
        entrypoints_package_root = Path(self.build_lib) / "entrypoints"
        target_key = os.environ.get("VIBESYS_WHEEL_TARGET")
        bundle_value = os.environ.get("VIBESYS_TUI_BUNDLE")
        bundle = Path(bundle_value) if bundle_value else None
        required = target_key is not None
        stage_prebuilt_tui(
            bundle,
            entrypoints_package_root / "_tui",
            required=required,
            expected_target=target_key,
            expected_distribution_version=self.distribution.get_version(),
        )
        stage_resources(_REPO_ROOT, vibesys_package_root / "_resources", required=required)
        stage_sdk(_REPO_ROOT, vibesys_package_root / "_sdk", required=required)


class BdistWheel(_bdist_wheel):
    """Emit a native payload wheel with an explicit cross-platform Python tag."""

    _release_target: WheelTarget | None = None

    def finalize_options(self) -> None:
        """Apply the requested release target before wheel paths are finalized."""
        super().finalize_options()
        target_key = os.environ.get("VIBESYS_WHEEL_TARGET")
        if target_key is not None:
            target = resolve_wheel_target(
                target_key,
                host_system=platform.system(),
                host_machine=platform.machine(),
            )
            self._release_target = target
            self.root_is_pure = False
            self.plat_name = target.wheel_platform
            self.plat_name_supplied = True

    def get_tag(self) -> tuple[str, str, str]:
        """Return the configured release platform or the default development tag."""
        if self._release_target is None:
            return super().get_tag()
        return ("py3", "none", self._release_target.wheel_platform)


class ReleaseDistribution(_Distribution):
    """Select platlib only for wheels that contain a native release payload."""

    def has_ext_modules(self) -> bool:
        """Make release packages install at wheel root so audit tools can inspect them."""
        return release_has_native_payload()


packages, package_dirs = discover_distribution_packages(_REPO_ROOT)
setup(
    packages=packages,
    package_dir=package_dirs,
    cmdclass={"bdist_wheel": BdistWheel, "build_py": BuildPy},
    distclass=ReleaseDistribution,
)
