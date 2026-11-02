"""The prove-or-block layer of run recovery stays deleted (#3475).

A run that cannot tell what an interruption left behind runs the step again, and
``clear`` makes that safe (``docs/contributing/run-recovery.md``). The layer that proved or
blocked instead (recovery barriers, inspections, receipts, effect fences, the lease and the
fence) is deleted module by module. Each deleting pull request adds what it removed to
``DELETED_MODULES``, ``DELETED_SYMBOLS`` or ``DELETED_MEMBERS`` below, one line each, so the layer
cannot come back under the same name. Unenforced remainder: a new mechanism under a different name that does
the same job; review applies the rule "no result means run again".
"""

from __future__ import annotations

import ast
import keyword
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

DELETED_MODULES: tuple[str, ...] = (
    # The keyed-replay driver and its pure half (#3492, #3475 part 7a).
    "vibesys.serial_replay",
    "vs_core.api.replay",
    "vs_core.flows._replay_accounting",
    "vs_core.flows._replay_archive",
    "vs_core.flows._replay_causality",
    "vs_core.flows._replay_controls",
    "vs_core.flows._replay_history",
    "vs_core.flows._replay_identity",
    "vs_core.flows._replay_iterations",
    "vs_core.flows._replay_physical",
    "vs_core.flows._replay_resources",
    "vs_core.flows._replay_run",
    "vs_core.flows._replay_schema",
    "vs_core.flows._replay_sources",
    "vs_core.flows._replay_turns",
    "vs_core.flows._replay_writers",
    "vs_runtime._replay_controls",
    "vs_runtime._replay_evaluation",
    "vs_runtime._replay_evaluation_jobs",
    "vs_runtime._replay_evidence",
    "vs_runtime._replay_executors",
    "vs_runtime._replay_fake",
    "vs_runtime._replay_launch",
    "vs_runtime._replay_operations",
    "vs_runtime._replay_owner",
    "vs_runtime._replay_prompts",
    "vs_runtime._replay_run",
    "vs_runtime._replay_runtime",
    "vs_runtime._replay_sessions",
    "vs_runtime._replay_tools",
    "vs_runtime._replay_wiring",
    "vs_runtime._replay_workspaces",
    # The old shell runtime, which production left for the step loop (#3541, #3475 part 6b).
    "vs_runtime._core_loop",
    "vs_runtime._core_publications",
    "vs_runtime._core_run",
    "vs_runtime._core_wiring",
    # The keyed replay store, which only its own contract suite used (#3475 part 6b).
    "vs_runtime._replay_store",
    # The state store's lease, fence and compare-and-swap record (#3475 part 6b).
    "vs_project._store_operations",
    "vs_project._decode_memo",
    # The dormant replay recovery: writer proofs, evaluation workspace acquisition and the
    # replay store (#3475 6b). No production wiring passed any of them.
    "vs_runtime._evaluation_recovery",
    "vs_runtime._writer_reconciliation",
    "vs_runtime.api.replay",
    # The receipt-backed executor of registered operations, the BlockRequest publisher and the
    # InspectRequest answer (#3475 6b): a registered operation runs on OperationStepRunner.
    "vs_runtime._operation_receipts",
    "vs_runtime._operation_requests",
    "vs_runtime._request_inspection",
    "vs_runtime._semantic_events",
    # The effect fences: an executor entered one before it created something, and a cancel,
    # close or discard closed it before answering "nothing held" (#3475 6b). The step loop's
    # scope order and clear replaced them.
    "vs_runtime._effect_fence",
    "vs_runtime.api.fence_contracts",
    # The receipt store: what it kept is the keyed document store, now named for that.
    "vs_runtime._receipt_store",
    # The restart reconciliation: no process emitted `RecoveryStarted` since the step loop (#3475 R5).
    "vs_core._request_recovery",
    "vs_core._recovery_owners",
    # The host epoch log: the records state kept `hosts` constant empty since the step loop (#3475 R5).
    "vs_core._host_epochs",
)
"""Dotted module paths that no file under ``src/`` or ``libs/*/src`` may provide, as a module or
as a package (a listed ``a.b`` also forbids ``a.b.c``)."""

DELETED_SYMBOLS: tuple[str, ...] = (
    "ReplayUnknown",
    # The old shell runtime (#3475 part 6b).
    "CoreRunHost",
    "CoreRuntime",
    "CoreRuntimeBindings",
    "JournalPublicationDelivery",
    "LeaseUnavailableError",
    "ProductionCoreTransitions",
    "RunStalledError",
    "core_bindings",
    "drive_core",
    "start_core",
    # The state store's writer half (#3475 part 6b), and its fence once the writer-proof modules went.
    "CommitFault",
    "CommitOutcome",
    "ConflictReason",
    "LeaseDocument",
    "RunLogReplayStore",
    "StateStoreWriteError",
    "StoreFence",
    "StoreOperations",
    "mutate_if_fenced",
    # The receipt store's execution rule: replay, identity, authority and the begun marker.
    "BegunUnsealed",
    "ExecutionHistory",
    "NeverBegun",
    "Performer",
    "SealedExecution",
    "run_once",
    # The effect fences.
    "EffectFences",
    "FenceKey",
    "FenceKind",
    "MayHold",
    "NothingHeld",
    "never_held",
    # The kernel's own end of a step at its limit (#3475 R4): `decide` is the one mechanism.
    "ReconciliationDeadline",
    "ReconciliationCommandLapsed",
    "_lapse_command",
    "_end_open_record",
    "_lost_turn",
    "_orphaned_jobs",
    "_orphan_source",
    "_submission_ended",
    "_heard_by_decision",
    "_deadline_barrier",
    "_deadline_target",
    "_awaiting_report",
    "_pending_cancellations",
    "_pending_probes",
    "_inspection_exists",
    "_child_release_signals",
    "_cancelled_here",
    "_sourced",
    # The restart reconciliation (#3475 R5): a killed run restarts through `fold` and `decide`.
    "RecoveryStarted",
    "RecoveryReady",
    "RecoveryCheckWaitLapsed",
    "EpochResumed",
    "open_host_epoch",
    "unanswered_measures",
    "credit_downtime",
    # The branches that read the constant recovery, hosts and children state (#3475 R5 group B).
    "recovery_waits",
    "record_bound",
    "_check_waits",
    "_child_dependencies",
    "_child_release_confirmed",
    "_child_released",
    "_downtime_within",
    "_historical_child_owned",
    "_known_child_observation",
    "_released_child",
    "_released_child_source",
    # The records state the step loop left constant (#3475 R5 group B, envelope version 44).
    "ChildLease",
    "ChildObservationWatermark",
    "HOST_LOG_LENGTH",
    "HostEpoch",
    "HostRenewed",
    "RecoveryBarrier",
    "RecoveryCheck",
    "RecoveryPhase",
    # The wait's host-downtime credit, always 0 since the run clock excludes downtime (#3475 D11, version 45).
    "downtime_credit",
    # The envelope's fence, read by no code (#3475 D11, version 46); retained receipts keep their own model.
    "HostFence",
)
"""Names that no file under ``src/`` or ``libs/*/src`` may define or import."""

DELETED_MEMBERS: tuple[tuple[str, str], ...] = ()
"""``(class, member)`` pairs: no class of that name under ``src/`` or ``libs/*/src`` may define a
member of that name, as an enum member, a field or a method. For a value that leaves a type the
type keeps, such as ``ObservationStatus.UNKNOWN``, where listing the member's name alone would ban
it from every other class. A deleting pull request adds one line here. The scan reads Python, so
a ``.proto`` value is held by its generated class, which changes with the proto."""

_ROOT = Path(__file__).resolve().parents[2]
_RULE = "docs/contributing/run-recovery.md, 'Keeping the deleted modules from coming back'"


def module_at(path: PurePosixPath) -> str | None:
    """The dotted module the product file at repository path *path* provides, else None.

    Product code is ``src/<package>/...`` and ``libs/<library>/src/<package>/...``; a package's
    ``__init__.py`` provides the package.
    """
    parts = path.with_suffix("").parts
    if parts[:1] == ("src",):
        below = parts[1:]
    elif parts[:1] == ("libs",) and parts[2:3] == ("src",):
        below = parts[3:]
    else:
        return None
    return ".".join(below[:-1] if below[-1:] == ("__init__",) else below) or None


def deleted_modules_present(
    modules: dict[str, str], deleted: tuple[str, ...] = DELETED_MODULES
) -> list[str]:
    """One message per file of *modules* (path to dotted module) that provides a deleted module."""
    return [
        f"{path}: provides `{name}`, which the prove-or-block cleanup deleted ({_RULE}). Do not"
        " bring it back; if the concept is wanted again, remove the name from DELETED_MODULES"
        " in the pull request that argues for it."
        for path, name in sorted(modules.items())
        for gone in deleted
        if name == gone or name.startswith(f"{gone}.")
    ]


def deleted_symbols_defined(
    path: str, source: str, deleted: tuple[str, ...] = DELETED_SYMBOLS
) -> list[str]:
    """One message per line of *source* that defines or imports a deleted name."""
    names = frozenset(deleted)
    found: list[tuple[int, str, str]] = []
    for node in walk_nodes(parse_source(source, path)):
        match node:
            case ast.ClassDef() | ast.FunctionDef() | ast.AsyncFunctionDef():
                found.append((node.lineno, node.name, "defines"))
            case ast.TypeAlias():
                found.append((node.lineno, node.name.id, "defines"))
            case ast.Assign():
                found.extend(
                    (node.lineno, target.id, "defines")
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )
            case ast.AnnAssign(target=ast.Name(id=name)):
                found.append((node.lineno, name, "defines"))
            case ast.Import() | ast.ImportFrom():
                for alias in node.names:
                    found.append((node.lineno, alias.name.rsplit(".", 1)[-1], "imports"))
                    if alias.asname is not None:
                        found.append((node.lineno, alias.asname, "imports"))
    return [
        f"{path}:{line}: {how} `{name}`, which the prove-or-block cleanup deleted ({_RULE}). Do"
        " not bring it back; if the concept is wanted again, remove the name from DELETED_SYMBOLS"
        " in the pull request that argues for it."
        for line, name, how in sorted(found)
        if name in names
    ]


def deleted_members_defined(
    path: str, source: str, deleted: tuple[tuple[str, str], ...] = DELETED_MEMBERS
) -> list[str]:
    """One message per line of *source* that defines a deleted member of a class of that name."""
    members: dict[str, set[str]] = {}
    for owner, member in deleted:
        members.setdefault(owner, set()).add(member)
    found: list[tuple[int, str, str]] = []
    for node in walk_nodes(parse_source(source, path)):
        if not (isinstance(node, ast.ClassDef) and node.name in members):
            continue
        for statement in node.body:
            match statement:
                case ast.Assign():
                    names = [
                        (statement.lineno, target.id)
                        for target in statement.targets
                        if isinstance(target, ast.Name)
                    ]
                case ast.AnnAssign(target=ast.Name(id=name)):
                    names = [(statement.lineno, name)]
                case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                    names = [(statement.lineno, statement.name)]
                case _:
                    names = []
            found.extend(
                (line, node.name, name) for line, name in names if name in members[node.name]
            )
    return [
        f"{path}:{line}: `{owner}` defines `{member}`, which the prove-or-block cleanup deleted"
        f" ({_RULE}). Do not bring it back; if the concept is wanted again, remove the pair from"
        " DELETED_MEMBERS in the pull request that argues for it."
        for line, owner, member in sorted(found)
    ]


def _product_files(root: Path) -> dict[str, Path]:
    """Each product source file, by its repository path."""
    return {
        str(path.relative_to(root)): path
        for base in (root / "src", *sorted((root / "libs").glob("*/src")))
        for path in sorted(base.rglob("*.py"))
    }


def test_no_deleted_module_is_provided_by_the_product() -> None:
    modules = {
        path: module
        for path in _product_files(_ROOT)
        if (module := module_at(PurePosixPath(path))) is not None
    }
    violations = deleted_modules_present(modules)
    assert not violations, "\n".join(violations)


def test_no_deleted_name_is_defined_or_imported_by_the_product() -> None:
    violations = [
        message
        for path, location in _product_files(_ROOT).items()
        if any(name in (text := location.read_text(encoding="utf-8")) for name in DELETED_SYMBOLS)
        for message in deleted_symbols_defined(path, text)
    ]
    assert not violations, "\n".join(violations)


def test_no_deleted_member_is_defined_by_the_product() -> None:
    owners = {owner for owner, _ in DELETED_MEMBERS}
    violations = [
        message
        for path, location in _product_files(_ROOT).items()
        if any(owner in (text := location.read_text(encoding="utf-8")) for owner in owners)
        for message in deleted_members_defined(path, text)
    ]
    assert not violations, "\n".join(violations)


def test_every_listed_name_is_a_plain_dotted_path_or_identifier() -> None:
    assert len(set(DELETED_MODULES)) == len(DELETED_MODULES)
    assert len(set(DELETED_SYMBOLS)) == len(DELETED_SYMBOLS)
    assert all(part.isidentifier() for name in DELETED_MODULES for part in name.split(".")), (
        "DELETED_MODULES holds dotted module paths"
    )
    assert all(name.isidentifier() for name in DELETED_SYMBOLS), "DELETED_SYMBOLS holds names"
    assert len(set(DELETED_MEMBERS)) == len(DELETED_MEMBERS)
    assert all(
        owner.isidentifier() and member.isidentifier() for owner, member in DELETED_MEMBERS
    ), "DELETED_MEMBERS holds (class, member) pairs of names"


def _layout(module: str, *, package: bool) -> str:
    """The repository path at which a file would provide *module*."""
    parts = module.split(".")
    leaf = [*parts, "__init__"] if package else parts
    base = "src" if parts[0] == "vibesys" else f"libs/{parts[0].replace('_', '-')}/src"
    return f"{base}/{'/'.join(leaf)}.py"


@given(module=st.sampled_from(DELETED_MODULES), package=st.booleans())
def test_every_deleted_module_fails_the_guard_wherever_it_is_brought_back(
    *, module: str, package: bool
) -> None:
    path = _layout(module, package=package)
    provided = module_at(PurePosixPath(path))
    assert provided == module
    assert len(deleted_modules_present({path: provided})) == 1


@given(st.sampled_from(DELETED_MODULES), st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True))
def test_a_module_below_a_deleted_package_fails_the_guard(module: str, child: str) -> None:
    assert len(deleted_modules_present({"p.py": f"{module}.{child}"})) == 1


@given(
    st.sampled_from(DELETED_MODULES),
    st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True),
)
def test_a_neighbour_that_only_shares_a_prefix_passes_the_guard(module: str, other: str) -> None:
    assert deleted_modules_present({"p.py": f"{module}_x9"}) == []
    assert deleted_modules_present({"p.py": f"zz_{other}.{module}"}) == []


@pytest.mark.parametrize(
    "source",
    [
        "class Gone: ...",
        "def Gone(): ...",
        "async def Gone(): ...",
        "Gone = 1",
        "Gone: int = 1",
        "type Gone = int",
        "from vs_core.api import Gone",
        "from vs_core.api import Other as Gone",
        "from vs_core.api import Gone as Other",
        "import vs_core.api.Gone",
        "import vs_core as Gone",
        "class Box:\n    Gone = 1",
        "if True:\n    def Gone(): ...",
    ],
)
def test_the_scan_sees_every_way_to_define_or_import_a_name(source: str) -> None:
    assert len(deleted_symbols_defined("p.py", source, deleted=("Gone",))) >= 1


@pytest.mark.parametrize(
    "source",
    [
        "class Other: ...",
        "x = Gone  # a use needs an import or a definition elsewhere",
        "'''Gone is mentioned in prose.'''",
        "from vs_core.api import Other",
        "value = other.Gone",
        "def f(Gone2): ...",
    ],
)
def test_the_scan_leaves_a_mention_or_a_use_alone(source: str) -> None:
    assert deleted_symbols_defined("p.py", source, deleted=("Gone",)) == []


def test_a_failure_names_the_file_the_line_and_the_fix() -> None:
    [module] = deleted_modules_present(
        {"libs/vs-core/src/vs_core/gone.py": "vs_core.gone"}, deleted=("vs_core.gone",)
    )
    [symbol] = deleted_symbols_defined("src/a.py", "x = 1\nclass Gone: ...", deleted=("Gone",))
    assert module.startswith("libs/vs-core/src/vs_core/gone.py: provides `vs_core.gone`")
    assert "DELETED_MODULES" in module
    assert symbol.startswith("src/a.py:2: defines `Gone`")
    assert "DELETED_SYMBOLS" in symbol


def test_a_package_init_provides_its_package_and_other_paths_provide_nothing() -> None:
    assert module_at(PurePosixPath("libs/vs-core/src/vs_core/api/__init__.py")) == "vs_core.api"
    assert module_at(PurePosixPath("libs/vs-core/src/vs_core/_a.py")) == "vs_core._a"
    assert module_at(PurePosixPath("src/vibesys/_a.py")) == "vibesys._a"
    assert module_at(PurePosixPath("libs/vs-core/tests/test_a.py")) is None
    assert module_at(PurePosixPath("tests/architecture/test_a.py")) is None


_IDENTIFIERS = st.from_regex(r"[A-Za-z][A-Za-z0-9_]{0,10}", fullmatch=True).filter(
    lambda name: not keyword.iskeyword(name)
)


@pytest.mark.parametrize(
    "body",
    [
        "{member} = 1",
        "{member}: int = 1",
        "{member}: int",
        "def {member}(self): ...",
        "async def {member}(self): ...",
        "class {member}: ...",
        "_decoy = 1\n    {member} = {member}2 = 3",
    ],
)
@example(owner="A", member="x")
@given(owner=_IDENTIFIERS, member=_IDENTIFIERS)
def test_a_member_is_found_in_every_form_a_class_can_define_it(
    body: str, owner: str, member: str
) -> None:
    source = f"class {owner}:\n    {body.format(member=member)}\n"
    found = deleted_members_defined("p.py", source, deleted=((owner, member),))
    assert len(found) == 1
    assert f"`{owner}` defines `{member}`" in found[0]


@example(owner="Ax", other="A", member="x")
@example(owner="A", other="B", member="f")
@given(owner=_IDENTIFIERS, other=_IDENTIFIERS, member=_IDENTIFIERS)
def test_the_same_member_in_another_class_or_a_use_of_it_passes(
    owner: str, other: str, member: str
) -> None:
    source = (
        f"class _{other}:\n    {member} = 1\n"
        f"class {owner}:\n    _kept = {member}\n    def _f(self):\n        return self.{member}\n"
        f"value = {owner}.{member}\n"
    )
    assert deleted_members_defined("p.py", source, deleted=((owner, member),)) == []


@given(owner=_IDENTIFIERS, member=_IDENTIFIERS)
def test_a_member_defined_below_a_nested_class_of_the_same_name_is_found(
    owner: str, member: str
) -> None:
    source = f"class _Outer:\n    class {owner}:\n        {member} = 1\n"
    assert len(deleted_members_defined("p.py", source, deleted=((owner, member),))) == 1


def test_a_member_failure_names_the_file_the_line_and_the_fix() -> None:
    [message] = deleted_members_defined(
        "src/a.py",
        "x = 1\nclass Status:\n    UNKNOWN = 'unknown'\n",
        deleted=(("Status", "UNKNOWN"),),
    )
    assert message.startswith("src/a.py:3: `Status` defines `UNKNOWN`")
    assert "DELETED_MEMBERS" in message
