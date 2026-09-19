import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from server.wire.v2 import common_pb2 as _common_pb2
from server.wire.v2 import events_pb2 as _events_pb2
from server.wire.v2 import snapshot_pb2 as _snapshot_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CommandAction(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMMAND_ACTION_UNSPECIFIED: _ClassVar[CommandAction]
    COMMAND_ACTION_PAUSE: _ClassVar[CommandAction]
    COMMAND_ACTION_RESUME: _ClassVar[CommandAction]
    COMMAND_ACTION_STEER: _ClassVar[CommandAction]
    COMMAND_ACTION_STOP: _ClassVar[CommandAction]

class CommandAckStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMMAND_ACK_STATUS_UNSPECIFIED: _ClassVar[CommandAckStatus]
    COMMAND_ACK_STATUS_PENDING: _ClassVar[CommandAckStatus]
    COMMAND_ACK_STATUS_CONSUMED: _ClassVar[CommandAckStatus]

class ObjectiveDirection(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OBJECTIVE_DIRECTION_UNSPECIFIED: _ClassVar[ObjectiveDirection]
    OBJECTIVE_DIRECTION_MAX: _ClassVar[ObjectiveDirection]
    OBJECTIVE_DIRECTION_MIN: _ClassVar[ObjectiveDirection]

class HypothesisOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HYPOTHESIS_OUTCOME_UNSPECIFIED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_CONTINUE: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_SUPPORTED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_NOMINATED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_DISPROVEN: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_IMPLEMENTATION_FAILED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_INCONCLUSIVE: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_BLOCKED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_PROVEN: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_REJECTED: _ClassVar[HypothesisOutcome]
    HYPOTHESIS_OUTCOME_UNMEASURED: _ClassVar[HypothesisOutcome]

class RoundReviewVerdict(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROUND_REVIEW_VERDICT_UNSPECIFIED: _ClassVar[RoundReviewVerdict]
    ROUND_REVIEW_VERDICT_PASS: _ClassVar[RoundReviewVerdict]
    ROUND_REVIEW_VERDICT_FAIL: _ClassVar[RoundReviewVerdict]
    ROUND_REVIEW_VERDICT_DEFERRED: _ClassVar[RoundReviewVerdict]

class CandidateDisposition(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CANDIDATE_DISPOSITION_UNSPECIFIED: _ClassVar[CandidateDisposition]
    CANDIDATE_DISPOSITION_UNASSESSED: _ClassVar[CandidateDisposition]
    CANDIDATE_DISPOSITION_DISCARD: _ClassVar[CandidateDisposition]
    CANDIDATE_DISPOSITION_PREREQUISITE: _ClassVar[CandidateDisposition]
    CANDIDATE_DISPOSITION_PARETO_FRONTIER: _ClassVar[CandidateDisposition]

class PerfDeltaReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PERF_DELTA_REASON_UNSPECIFIED: _ClassVar[PerfDeltaReason]
    PERF_DELTA_REASON_NO_BASELINE_YET: _ClassVar[PerfDeltaReason]
    PERF_DELTA_REASON_BASELINE_UNRESOLVED: _ClassVar[PerfDeltaReason]
    PERF_DELTA_REASON_NOT_FRAMEWORK_MEASURED: _ClassVar[PerfDeltaReason]

class StrategyDisposition(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STRATEGY_DISPOSITION_UNSPECIFIED: _ClassVar[StrategyDisposition]
    STRATEGY_DISPOSITION_AVAILABLE: _ClassVar[StrategyDisposition]
    STRATEGY_DISPOSITION_PARKED: _ClassVar[StrategyDisposition]
    STRATEGY_DISPOSITION_ABANDONED: _ClassVar[StrategyDisposition]

class DesignChange(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DESIGN_CHANGE_UNSPECIFIED: _ClassVar[DesignChange]
    DESIGN_CHANGE_ADDED: _ClassVar[DesignChange]
    DESIGN_CHANGE_MODIFIED: _ClassVar[DesignChange]
    DESIGN_CHANGE_DELETED: _ClassVar[DesignChange]
    DESIGN_CHANGE_RENAMED: _ClassVar[DesignChange]

class ChatModelSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHAT_MODEL_SOURCE_UNSPECIFIED: _ClassVar[ChatModelSource]
    CHAT_MODEL_SOURCE_RUN: _ClassVar[ChatModelSource]
    CHAT_MODEL_SOURCE_ROLE: _ClassVar[ChatModelSource]
    CHAT_MODEL_SOURCE_SUGGESTED: _ClassVar[ChatModelSource]

class RepositoryVisibility(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REPOSITORY_VISIBILITY_UNSPECIFIED: _ClassVar[RepositoryVisibility]
    REPOSITORY_VISIBILITY_PRIVATE: _ClassVar[RepositoryVisibility]
    REPOSITORY_VISIBILITY_PUBLIC: _ClassVar[RepositoryVisibility]
    REPOSITORY_VISIBILITY_INTERNAL: _ClassVar[RepositoryVisibility]

class TuiTheme(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TUI_THEME_UNSPECIFIED: _ClassVar[TuiTheme]
    TUI_THEME_DARK: _ClassVar[TuiTheme]
    TUI_THEME_LIGHT: _ClassVar[TuiTheme]
    TUI_THEME_SOLARIZED_DARK: _ClassVar[TuiTheme]
    TUI_THEME_SOLARIZED_LIGHT: _ClassVar[TuiTheme]
    TUI_THEME_CATPPUCCIN_MOCHA: _ClassVar[TuiTheme]
    TUI_THEME_CATPPUCCIN_LATTE: _ClassVar[TuiTheme]
    TUI_THEME_HIGH_CONTRAST_DARK: _ClassVar[TuiTheme]
    TUI_THEME_HIGH_CONTRAST_LIGHT: _ClassVar[TuiTheme]
COMMAND_ACTION_UNSPECIFIED: CommandAction
COMMAND_ACTION_PAUSE: CommandAction
COMMAND_ACTION_RESUME: CommandAction
COMMAND_ACTION_STEER: CommandAction
COMMAND_ACTION_STOP: CommandAction
COMMAND_ACK_STATUS_UNSPECIFIED: CommandAckStatus
COMMAND_ACK_STATUS_PENDING: CommandAckStatus
COMMAND_ACK_STATUS_CONSUMED: CommandAckStatus
OBJECTIVE_DIRECTION_UNSPECIFIED: ObjectiveDirection
OBJECTIVE_DIRECTION_MAX: ObjectiveDirection
OBJECTIVE_DIRECTION_MIN: ObjectiveDirection
HYPOTHESIS_OUTCOME_UNSPECIFIED: HypothesisOutcome
HYPOTHESIS_OUTCOME_CONTINUE: HypothesisOutcome
HYPOTHESIS_OUTCOME_SUPPORTED: HypothesisOutcome
HYPOTHESIS_OUTCOME_NOMINATED: HypothesisOutcome
HYPOTHESIS_OUTCOME_DISPROVEN: HypothesisOutcome
HYPOTHESIS_OUTCOME_IMPLEMENTATION_FAILED: HypothesisOutcome
HYPOTHESIS_OUTCOME_INCONCLUSIVE: HypothesisOutcome
HYPOTHESIS_OUTCOME_BLOCKED: HypothesisOutcome
HYPOTHESIS_OUTCOME_PROVEN: HypothesisOutcome
HYPOTHESIS_OUTCOME_REJECTED: HypothesisOutcome
HYPOTHESIS_OUTCOME_UNMEASURED: HypothesisOutcome
ROUND_REVIEW_VERDICT_UNSPECIFIED: RoundReviewVerdict
ROUND_REVIEW_VERDICT_PASS: RoundReviewVerdict
ROUND_REVIEW_VERDICT_FAIL: RoundReviewVerdict
ROUND_REVIEW_VERDICT_DEFERRED: RoundReviewVerdict
CANDIDATE_DISPOSITION_UNSPECIFIED: CandidateDisposition
CANDIDATE_DISPOSITION_UNASSESSED: CandidateDisposition
CANDIDATE_DISPOSITION_DISCARD: CandidateDisposition
CANDIDATE_DISPOSITION_PREREQUISITE: CandidateDisposition
CANDIDATE_DISPOSITION_PARETO_FRONTIER: CandidateDisposition
PERF_DELTA_REASON_UNSPECIFIED: PerfDeltaReason
PERF_DELTA_REASON_NO_BASELINE_YET: PerfDeltaReason
PERF_DELTA_REASON_BASELINE_UNRESOLVED: PerfDeltaReason
PERF_DELTA_REASON_NOT_FRAMEWORK_MEASURED: PerfDeltaReason
STRATEGY_DISPOSITION_UNSPECIFIED: StrategyDisposition
STRATEGY_DISPOSITION_AVAILABLE: StrategyDisposition
STRATEGY_DISPOSITION_PARKED: StrategyDisposition
STRATEGY_DISPOSITION_ABANDONED: StrategyDisposition
DESIGN_CHANGE_UNSPECIFIED: DesignChange
DESIGN_CHANGE_ADDED: DesignChange
DESIGN_CHANGE_MODIFIED: DesignChange
DESIGN_CHANGE_DELETED: DesignChange
DESIGN_CHANGE_RENAMED: DesignChange
CHAT_MODEL_SOURCE_UNSPECIFIED: ChatModelSource
CHAT_MODEL_SOURCE_RUN: ChatModelSource
CHAT_MODEL_SOURCE_ROLE: ChatModelSource
CHAT_MODEL_SOURCE_SUGGESTED: ChatModelSource
REPOSITORY_VISIBILITY_UNSPECIFIED: RepositoryVisibility
REPOSITORY_VISIBILITY_PRIVATE: RepositoryVisibility
REPOSITORY_VISIBILITY_PUBLIC: RepositoryVisibility
REPOSITORY_VISIBILITY_INTERNAL: RepositoryVisibility
TUI_THEME_UNSPECIFIED: TuiTheme
TUI_THEME_DARK: TuiTheme
TUI_THEME_LIGHT: TuiTheme
TUI_THEME_SOLARIZED_DARK: TuiTheme
TUI_THEME_SOLARIZED_LIGHT: TuiTheme
TUI_THEME_CATPPUCCIN_MOCHA: TuiTheme
TUI_THEME_CATPPUCCIN_LATTE: TuiTheme
TUI_THEME_HIGH_CONTRAST_DARK: TuiTheme
TUI_THEME_HIGH_CONTRAST_LIGHT: TuiTheme

class CommandAck(_message.Message):
    __slots__ = ("action", "status")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    action: CommandAction
    status: CommandAckStatus
    def __init__(self, action: _Optional[_Union[CommandAction, str]] = ..., status: _Optional[_Union[CommandAckStatus, str]] = ...) -> None: ...

class ChatResult(_message.Message):
    __slots__ = ("question", "answer", "thread_id")
    QUESTION_FIELD_NUMBER: _ClassVar[int]
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    question: str
    answer: str
    thread_id: str
    def __init__(self, question: _Optional[str] = ..., answer: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class PerformanceRound(_message.Message):
    __slots__ = ("round", "perf_metric", "perf_unit", "passed", "profile_skipped")
    ROUND_FIELD_NUMBER: _ClassVar[int]
    PERF_METRIC_FIELD_NUMBER: _ClassVar[int]
    PERF_UNIT_FIELD_NUMBER: _ClassVar[int]
    PASSED_FIELD_NUMBER: _ClassVar[int]
    PROFILE_SKIPPED_FIELD_NUMBER: _ClassVar[int]
    round: int
    perf_metric: float
    perf_unit: str
    passed: bool
    profile_skipped: bool
    def __init__(self, round: _Optional[int] = ..., perf_metric: _Optional[float] = ..., perf_unit: _Optional[str] = ..., passed: _Optional[bool] = ..., profile_skipped: _Optional[bool] = ...) -> None: ...

class PerformanceContext(_message.Message):
    __slots__ = ("objective_metric", "objective_unit", "objective_direction", "objective_baseline_value", "objective_baseline_round", "objective_baseline_commit", "objective_description")
    OBJECTIVE_METRIC_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_UNIT_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_DIRECTION_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_BASELINE_VALUE_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_BASELINE_ROUND_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_BASELINE_COMMIT_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    objective_metric: str
    objective_unit: str
    objective_direction: ObjectiveDirection
    objective_baseline_value: float
    objective_baseline_round: int
    objective_baseline_commit: str
    objective_description: str
    def __init__(self, objective_metric: _Optional[str] = ..., objective_unit: _Optional[str] = ..., objective_direction: _Optional[_Union[ObjectiveDirection, str]] = ..., objective_baseline_value: _Optional[float] = ..., objective_baseline_round: _Optional[int] = ..., objective_baseline_commit: _Optional[str] = ..., objective_description: _Optional[str] = ...) -> None: ...

class HypothesisRound(_message.Message):
    __slots__ = ("round", "passed", "reviewed", "hypothesis_outcome", "judge_verdict", "perf_metric", "perf_unit", "perf_delta_pct", "commit", "official_evaluation", "candidate_disposition")
    ROUND_FIELD_NUMBER: _ClassVar[int]
    PASSED_FIELD_NUMBER: _ClassVar[int]
    REVIEWED_FIELD_NUMBER: _ClassVar[int]
    HYPOTHESIS_OUTCOME_FIELD_NUMBER: _ClassVar[int]
    JUDGE_VERDICT_FIELD_NUMBER: _ClassVar[int]
    PERF_METRIC_FIELD_NUMBER: _ClassVar[int]
    PERF_UNIT_FIELD_NUMBER: _ClassVar[int]
    PERF_DELTA_PCT_FIELD_NUMBER: _ClassVar[int]
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    OFFICIAL_EVALUATION_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_DISPOSITION_FIELD_NUMBER: _ClassVar[int]
    round: int
    passed: bool
    reviewed: bool
    hypothesis_outcome: HypothesisOutcome
    judge_verdict: RoundReviewVerdict
    perf_metric: float
    perf_unit: str
    perf_delta_pct: float
    commit: str
    official_evaluation: bool
    candidate_disposition: CandidateDisposition
    def __init__(self, round: _Optional[int] = ..., passed: _Optional[bool] = ..., reviewed: _Optional[bool] = ..., hypothesis_outcome: _Optional[_Union[HypothesisOutcome, str]] = ..., judge_verdict: _Optional[_Union[RoundReviewVerdict, str]] = ..., perf_metric: _Optional[float] = ..., perf_unit: _Optional[str] = ..., perf_delta_pct: _Optional[float] = ..., commit: _Optional[str] = ..., official_evaluation: _Optional[bool] = ..., candidate_disposition: _Optional[_Union[CandidateDisposition, str]] = ...) -> None: ...

class HypothesisEntry(_message.Message):
    __slots__ = ("hypothesis_id", "identified", "title", "claim", "action", "first_round", "last_round", "rounds", "resolved_outcome", "judge_verdict", "perf_metric", "perf_unit", "perf_delta_pct", "perf_metric_name", "perf_direction", "perf_baseline_value", "perf_baseline_round", "perf_baseline_commit", "perf_delta_reason", "kept", "strategy_disposition", "strategy_reason", "active")
    HYPOTHESIS_ID_FIELD_NUMBER: _ClassVar[int]
    IDENTIFIED_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CLAIM_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    FIRST_ROUND_FIELD_NUMBER: _ClassVar[int]
    LAST_ROUND_FIELD_NUMBER: _ClassVar[int]
    ROUNDS_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_OUTCOME_FIELD_NUMBER: _ClassVar[int]
    JUDGE_VERDICT_FIELD_NUMBER: _ClassVar[int]
    PERF_METRIC_FIELD_NUMBER: _ClassVar[int]
    PERF_UNIT_FIELD_NUMBER: _ClassVar[int]
    PERF_DELTA_PCT_FIELD_NUMBER: _ClassVar[int]
    PERF_METRIC_NAME_FIELD_NUMBER: _ClassVar[int]
    PERF_DIRECTION_FIELD_NUMBER: _ClassVar[int]
    PERF_BASELINE_VALUE_FIELD_NUMBER: _ClassVar[int]
    PERF_BASELINE_ROUND_FIELD_NUMBER: _ClassVar[int]
    PERF_BASELINE_COMMIT_FIELD_NUMBER: _ClassVar[int]
    PERF_DELTA_REASON_FIELD_NUMBER: _ClassVar[int]
    KEPT_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_DISPOSITION_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_REASON_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    hypothesis_id: str
    identified: bool
    title: str
    claim: str
    action: str
    first_round: int
    last_round: int
    rounds: _containers.RepeatedCompositeFieldContainer[HypothesisRound]
    resolved_outcome: str
    judge_verdict: _events_pb2.JudgeVerdict
    perf_metric: float
    perf_unit: str
    perf_delta_pct: float
    perf_metric_name: str
    perf_direction: ObjectiveDirection
    perf_baseline_value: float
    perf_baseline_round: int
    perf_baseline_commit: str
    perf_delta_reason: PerfDeltaReason
    kept: bool
    strategy_disposition: StrategyDisposition
    strategy_reason: str
    active: bool
    def __init__(self, hypothesis_id: _Optional[str] = ..., identified: _Optional[bool] = ..., title: _Optional[str] = ..., claim: _Optional[str] = ..., action: _Optional[str] = ..., first_round: _Optional[int] = ..., last_round: _Optional[int] = ..., rounds: _Optional[_Iterable[_Union[HypothesisRound, _Mapping]]] = ..., resolved_outcome: _Optional[str] = ..., judge_verdict: _Optional[_Union[_events_pb2.JudgeVerdict, str]] = ..., perf_metric: _Optional[float] = ..., perf_unit: _Optional[str] = ..., perf_delta_pct: _Optional[float] = ..., perf_metric_name: _Optional[str] = ..., perf_direction: _Optional[_Union[ObjectiveDirection, str]] = ..., perf_baseline_value: _Optional[float] = ..., perf_baseline_round: _Optional[int] = ..., perf_baseline_commit: _Optional[str] = ..., perf_delta_reason: _Optional[_Union[PerfDeltaReason, str]] = ..., kept: _Optional[bool] = ..., strategy_disposition: _Optional[_Union[StrategyDisposition, str]] = ..., strategy_reason: _Optional[str] = ..., active: _Optional[bool] = ...) -> None: ...

class ExperimentUpdate(_message.Message):
    __slots__ = ("run_id", "projection_id", "from_revision", "through_revision", "reset", "removed_hypothesis_ids")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_ID_FIELD_NUMBER: _ClassVar[int]
    FROM_REVISION_FIELD_NUMBER: _ClassVar[int]
    THROUGH_REVISION_FIELD_NUMBER: _ClassVar[int]
    RESET_FIELD_NUMBER: _ClassVar[int]
    REMOVED_HYPOTHESIS_IDS_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    projection_id: str
    from_revision: int
    through_revision: int
    reset: bool
    removed_hypothesis_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, run_id: _Optional[str] = ..., projection_id: _Optional[str] = ..., from_revision: _Optional[int] = ..., through_revision: _Optional[int] = ..., reset: _Optional[bool] = ..., removed_hypothesis_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class DesignFileChange(_message.Message):
    __slots__ = ("path", "change", "renamed_from")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CHANGE_FIELD_NUMBER: _ClassVar[int]
    RENAMED_FROM_FIELD_NUMBER: _ClassVar[int]
    path: str
    change: DesignChange
    renamed_from: str
    def __init__(self, path: _Optional[str] = ..., change: _Optional[_Union[DesignChange, str]] = ..., renamed_from: _Optional[str] = ...) -> None: ...

class DesignRound(_message.Message):
    __slots__ = ("round", "commit", "base", "files")
    ROUND_FIELD_NUMBER: _ClassVar[int]
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    round: int
    commit: str
    base: str
    files: DesignFiles
    def __init__(self, round: _Optional[int] = ..., commit: _Optional[str] = ..., base: _Optional[str] = ..., files: _Optional[_Union[DesignFiles, _Mapping]] = ...) -> None: ...

class DesignFiles(_message.Message):
    __slots__ = ("changes",)
    CHANGES_FIELD_NUMBER: _ClassVar[int]
    changes: _containers.RepeatedCompositeFieldContainer[DesignFileChange]
    def __init__(self, changes: _Optional[_Iterable[_Union[DesignFileChange, _Mapping]]] = ...) -> None: ...

class DesignPatch(_message.Message):
    __slots__ = ("base", "head", "path", "renamed_from", "patch", "truncated")
    BASE_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    RENAMED_FROM_FIELD_NUMBER: _ClassVar[int]
    PATCH_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    base: str
    head: str
    path: str
    renamed_from: str
    patch: str
    truncated: bool
    def __init__(self, base: _Optional[str] = ..., head: _Optional[str] = ..., path: _Optional[str] = ..., renamed_from: _Optional[str] = ..., patch: _Optional[str] = ..., truncated: _Optional[bool] = ...) -> None: ...

class ChatModelOption(_message.Message):
    __slots__ = ("model", "source", "default")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_FIELD_NUMBER: _ClassVar[int]
    model: str
    source: ChatModelSource
    default: bool
    def __init__(self, model: _Optional[str] = ..., source: _Optional[_Union[ChatModelSource, str]] = ..., default: _Optional[bool] = ...) -> None: ...

class ChatProviderOptions(_message.Message):
    __slots__ = ("provider", "models")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODELS_FIELD_NUMBER: _ClassVar[int]
    provider: str
    models: _containers.RepeatedCompositeFieldContainer[ChatModelOption]
    def __init__(self, provider: _Optional[str] = ..., models: _Optional[_Iterable[_Union[ChatModelOption, _Mapping]]] = ...) -> None: ...

class ChatOptions(_message.Message):
    __slots__ = ("providers",)
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    providers: _containers.RepeatedCompositeFieldContainer[ChatProviderOptions]
    def __init__(self, providers: _Optional[_Iterable[_Union[ChatProviderOptions, _Mapping]]] = ...) -> None: ...

class TuiDefaults(_message.Message):
    __slots__ = ("runs_dir", "input_path", "experiment_name", "repository_owner", "repository_name", "visibility", "theme")
    RUNS_DIR_FIELD_NUMBER: _ClassVar[int]
    INPUT_PATH_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_OWNER_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_NAME_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    THEME_FIELD_NUMBER: _ClassVar[int]
    runs_dir: str
    input_path: str
    experiment_name: str
    repository_owner: str
    repository_name: str
    visibility: RepositoryVisibility
    theme: TuiTheme
    def __init__(self, runs_dir: _Optional[str] = ..., input_path: _Optional[str] = ..., experiment_name: _Optional[str] = ..., repository_owner: _Optional[str] = ..., repository_name: _Optional[str] = ..., visibility: _Optional[_Union[RepositoryVisibility, str]] = ..., theme: _Optional[_Union[TuiTheme, str]] = ...) -> None: ...

class Response(_message.Message):
    __slots__ = ("protocol_version", "request_id", "timestamp", "ok", "error", "diagnostic", "ack", "chat", "chat_thread", "chat_options", "tui_defaults", "snapshot", "events", "performance", "performance_context", "experiments", "experiment_update", "experiments_ready", "design", "design_ready", "design_patch")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTIC_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    CHAT_FIELD_NUMBER: _ClassVar[int]
    CHAT_THREAD_FIELD_NUMBER: _ClassVar[int]
    CHAT_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    TUI_DEFAULTS_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENTS_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENT_UPDATE_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENTS_READY_FIELD_NUMBER: _ClassVar[int]
    DESIGN_FIELD_NUMBER: _ClassVar[int]
    DESIGN_READY_FIELD_NUMBER: _ClassVar[int]
    DESIGN_PATCH_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    request_id: str
    timestamp: _timestamp_pb2.Timestamp
    ok: bool
    error: str
    diagnostic: _common_pb2.Diagnostic
    ack: CommandAck
    chat: ChatResult
    chat_thread: _snapshot_pb2.ChatThreadInfo
    chat_options: ChatOptions
    tui_defaults: TuiDefaults
    snapshot: _snapshot_pb2.RunSnapshot
    events: _containers.RepeatedCompositeFieldContainer[_events_pb2.RunEvent]
    performance: _containers.RepeatedCompositeFieldContainer[PerformanceRound]
    performance_context: PerformanceContext
    experiments: _containers.RepeatedCompositeFieldContainer[HypothesisEntry]
    experiment_update: ExperimentUpdate
    experiments_ready: bool
    design: _containers.RepeatedCompositeFieldContainer[DesignRound]
    design_ready: bool
    design_patch: DesignPatch
    def __init__(self, protocol_version: _Optional[int] = ..., request_id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ok: _Optional[bool] = ..., error: _Optional[str] = ..., diagnostic: _Optional[_Union[_common_pb2.Diagnostic, _Mapping]] = ..., ack: _Optional[_Union[CommandAck, _Mapping]] = ..., chat: _Optional[_Union[ChatResult, _Mapping]] = ..., chat_thread: _Optional[_Union[_snapshot_pb2.ChatThreadInfo, _Mapping]] = ..., chat_options: _Optional[_Union[ChatOptions, _Mapping]] = ..., tui_defaults: _Optional[_Union[TuiDefaults, _Mapping]] = ..., snapshot: _Optional[_Union[_snapshot_pb2.RunSnapshot, _Mapping]] = ..., events: _Optional[_Iterable[_Union[_events_pb2.RunEvent, _Mapping]]] = ..., performance: _Optional[_Iterable[_Union[PerformanceRound, _Mapping]]] = ..., performance_context: _Optional[_Union[PerformanceContext, _Mapping]] = ..., experiments: _Optional[_Iterable[_Union[HypothesisEntry, _Mapping]]] = ..., experiment_update: _Optional[_Union[ExperimentUpdate, _Mapping]] = ..., experiments_ready: _Optional[bool] = ..., design: _Optional[_Iterable[_Union[DesignRound, _Mapping]]] = ..., design_ready: _Optional[bool] = ..., design_patch: _Optional[_Union[DesignPatch, _Mapping]] = ...) -> None: ...
