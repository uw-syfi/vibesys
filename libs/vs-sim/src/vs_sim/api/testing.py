"""The test side of vs-sim: the deterministic scheduler, Fakes, waits and contract suites."""

from vs_sim.child import ChildDiedError, run_in_child
from vs_sim.contracts import (
    BlockingRunnerContract,
    ClockContract,
    ClockUnderTest,
    ProcessLauncherContract,
    ProcessUnderTest,
    RunnerUnderTest,
    SignalSourceContract,
    SignalSourceUnderTest,
    SleeperContract,
    SleeperUnderTest,
)
from vs_sim.crash import (
    Restarted,
    RestartLimitError,
    after_crash,
    evenly_spaced,
    first_of_each_kind,
    restart_until_done,
)
from vs_sim.fakes import (
    FakeProcessLauncher,
    FakeSignalSource,
    GatedBlockingRunner,
    InlineBlockingRunner,
    ProcessScript,
)
from vs_sim.gate import Gate, arrival, start_thread, wait_until_started, wait_until_started_sync
from vs_sim.manual import ManualClock
from vs_sim.seeds import (
    SCHEDULE_SEED_OPTION,
    SEED_OPTION,
    explore_seed,
    random_for,
    replay_hint,
    seed_for_test,
)
from vs_sim.sim import WORLDS, Sim, UnknownWorldError, WorldFactory, WorldRegistry
from vs_sim.states import Changes, wait_for_async_state, wait_for_state
from vs_sim.threads import TGKILL_SUPPORTED, non_main_thread_ids, send_to_thread
from vs_sim.trace import EventTrace, TraceEvent
from vs_sim.virtual import (
    WORKER_GUARD_S,
    VirtualClock,
    VirtualDeadlockError,
    VirtualTimeLimitError,
    current_virtual_clock,
    run_virtual,
)
from vs_sim.waits import (
    HANG_GUARD_S,
    accept_or_fail,
    get_or_fail,
    join_or_fail,
    stop_process,
    wait_or_fail,
)

__all__ = [
    "HANG_GUARD_S",
    "SCHEDULE_SEED_OPTION",
    "SEED_OPTION",
    "TGKILL_SUPPORTED",
    "WORKER_GUARD_S",
    "WORLDS",
    "BlockingRunnerContract",
    "Changes",
    "ChildDiedError",
    "ClockContract",
    "ClockUnderTest",
    "EventTrace",
    "FakeProcessLauncher",
    "FakeSignalSource",
    "Gate",
    "GatedBlockingRunner",
    "InlineBlockingRunner",
    "ManualClock",
    "ProcessLauncherContract",
    "ProcessScript",
    "ProcessUnderTest",
    "RestartLimitError",
    "Restarted",
    "RunnerUnderTest",
    "SignalSourceContract",
    "SignalSourceUnderTest",
    "Sim",
    "SleeperContract",
    "SleeperUnderTest",
    "TraceEvent",
    "UnknownWorldError",
    "VirtualClock",
    "VirtualDeadlockError",
    "VirtualTimeLimitError",
    "WorldFactory",
    "WorldRegistry",
    "accept_or_fail",
    "after_crash",
    "arrival",
    "current_virtual_clock",
    "evenly_spaced",
    "explore_seed",
    "first_of_each_kind",
    "get_or_fail",
    "join_or_fail",
    "non_main_thread_ids",
    "random_for",
    "replay_hint",
    "restart_until_done",
    "run_in_child",
    "run_virtual",
    "seed_for_test",
    "send_to_thread",
    "start_thread",
    "stop_process",
    "wait_for_async_state",
    "wait_for_state",
    "wait_or_fail",
    "wait_until_started",
    "wait_until_started_sync",
]
