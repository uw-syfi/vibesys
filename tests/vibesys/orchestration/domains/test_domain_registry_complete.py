"""Every domain is named, defined, registered, and laid out as the renderer reads it.

Adding a domain touches a ``DomainName`` member, a package under
``vibesys/domains/`` exporting a ``DomainDefinition``, the ``DOMAINS`` registry,
and the package's prompt directory. The renderer reads role files by name and
treats a missing one as "inject nothing", so a misspelled ``implementor.md``
or an undeclared modality directory is silently ignored. These checks fail CI
naming the domain and the missing or stray piece; every list comes from the
enums, the registry or the tree, so a new domain cannot slip past them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import vibesys.domains
from vibesys.constants import DomainName
from vibesys.domains.base import DOMAIN_ROLES, DomainDefinition
from vibesys.domains.registry import DOMAINS

_PACKAGES = Path(vibesys.domains.__file__).parent
_ROLE_FILES = frozenset(f"{role.value}.md" for role in DOMAIN_ROLES)
_MODALITY_DIR = "modality"
_DOCS = "README.md"
_DEFINITIONS = tuple(DOMAINS.values())


def test_every_domain_name_is_registered_under_its_own_name() -> None:
    missing = sorted(name.value for name in set(DomainName) - set(DOMAINS))
    misfiled = sorted(name.value for name, domain in DOMAINS.items() if domain.name is not name)

    assert not missing, f"DomainName members missing from DOMAINS: {missing}"
    assert not misfiled, f"DOMAINS keys whose definition names another domain: {misfiled}"


def test_every_domain_package_is_registered() -> None:
    packages = {
        path.name
        for path in _PACKAGES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    registered = {domain.prompt_dir.parent.name for domain in _DEFINITIONS}

    assert packages == registered, (
        f"domain packages not in DOMAINS: {sorted(packages - registered)}; "
        f"registered domains with no package: {sorted(registered - packages)}"
    )


@pytest.mark.parametrize("domain", _DEFINITIONS, ids=lambda d: d.name.value)
def test_prompt_dir_holds_only_role_files_docs_and_modalities(domain: DomainDefinition) -> None:
    assert domain.prompt_dir == _PACKAGES / domain.prompt_dir.parent.name / "prompts"
    entries = {path.name for path in domain.prompt_dir.iterdir()}
    stray = sorted(entries - _ROLE_FILES - {_DOCS, _MODALITY_DIR})

    assert not stray, (
        f"{domain.prompt_dir}: {stray} is no role file; role files are {sorted(_ROLE_FILES)}"
    )


@pytest.mark.parametrize("domain", _DEFINITIONS, ids=lambda d: d.name.value)
def test_declared_modalities_match_their_prompt_directories(domain: DomainDefinition) -> None:
    root = domain.prompt_dir / _MODALITY_DIR
    present = {path.name for path in root.iterdir()} if root.is_dir() else set()

    assert present == set(domain.modalities), (
        f"{domain.name.value}: modality directories {sorted(present)} differ from "
        f"declared modalities {sorted(domain.modalities)}"
    )
    assert (domain.default_modality is None) is (not domain.modalities)
    assert domain.default_modality is None or domain.default_modality in domain.modalities
    for modality in domain.modalities:
        files = {path.name for path in (root / modality).iterdir()}
        assert files <= _ROLE_FILES, f"{root / modality}: {sorted(files - _ROLE_FILES)}"
