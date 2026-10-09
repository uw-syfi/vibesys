import type {
  DesignFileChange,
  DesignRound,
  HypothesisEntry,
  HypothesisRound,
} from '@vibesys/backend-client';
import {
  type AgentPhase,
  hypothesisRoundNumbers as coreHypothesisRoundNumbers,
  experimentForRound,
  hasRunEnded,
  planningStageForPhase,
} from '@vibesys/core-state';
import type {
  ExperimentIndexItem,
  ExperimentLogState,
  HypothesisDetail,
  HypothesisPlanningActivity,
  HypothesisScope,
  SessionState,
} from './session-model.js';
import {visibleRoundNumber} from './session-model.js';

/** Pure reducers and selectors for the hypotheses-first experiment surface. */
export function openExperimentLog(state: SessionState): SessionState {
  const existing = state.experimentLog;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    hypothesisDetail: null,
    hypothesisScope: null,
    selectedRound: null,
    selectedAgentKind: null,
    experimentLog: existing ?? {entries: [], selectedId: null, pending: true, error: null},
  };
}

export function setExperiments(state: SessionState, entries: HypothesisEntry[]): SessionState {
  const log = state.experimentLog;
  if (log === null) return state;
  const activity = hypothesisPlanningActivity(state);
  const indexed = orderAndIndexExperiments(entries);
  const selection = reconcileExperimentSelection(log, indexed, activity);
  const unownedRounds = unownedExperimentRounds(state, indexed.entries);
  const refreshed = {
    ...state,
    hypothesisDetail: reconcileHypothesisDetail(state.hypothesisDetail, indexed.entries),
    experimentLog: {
      ...log,
      entries: indexed.entries,
      ...selection,
      selectedUnownedRound: reconcileUnownedRoundSelection(
        log.selectedUnownedRound,
        unownedRounds,
        indexed.entries.length === 0,
      ),
      pending: false,
      error: null,
    },
  };
  return refreshExperimentScope(state, refreshed, visibleRoundNumber(state));
}

function refreshExperimentScope(
  previous: SessionState,
  refreshed: SessionState,
  anchor: number | null,
): SessionState {
  if (previous.hypothesisScope === null || anchor === null) return refreshed;
  return {...refreshed, ...scopeStateForRound(refreshed, anchor)};
}

interface IndexedExperiments {
  entries: HypothesisEntry[];
  keys: string[];
}
function orderAndIndexExperiments(entries: HypothesisEntry[]): IndexedExperiments {
  const ordered = [...entries].sort(compareHypothesisEntries);
  return {entries: ordered, keys: ordered.map(entryKey)};
}
type ExperimentSelection = Pick<
  ExperimentLogState,
  'selectedId' | 'selectedActivity' | 'selectedActivityRound'
>;
function reconcileExperimentSelection(
  log: ExperimentLogState,
  indexed: IndexedExperiments,
  activity: HypothesisPlanningActivity | null,
): ExperimentSelection {
  const selectedActivityRound =
    log.selectedActivity === true
      ? (log.selectedActivityRound ?? activity?.roundNumber ?? null)
      : null;
  const materializedActivity =
    selectedActivityRound === null
      ? undefined
      : indexed.entries.find(entry => scopeRounds(entry).includes(selectedActivityRound));
  return {
    selectedId:
      materializedActivity !== undefined
        ? entryKeyFor(indexed.entries, materializedActivity)
        : log.selectedId !== null && indexed.keys.includes(log.selectedId)
          ? log.selectedId
          : (indexed.keys[indexed.entries.findIndex(entry => entry.active === true)] ??
            indexed.keys[0] ??
            null),
    selectedActivity: materializedActivity === undefined && log.selectedActivity === true,
    selectedActivityRound: materializedActivity === undefined ? selectedActivityRound : null,
  };
}
function reconcileUnownedRoundSelection(
  current: number | null | undefined,
  unownedRounds: number[],
  hasNoHypotheses: boolean,
): number | null {
  if (current !== undefined && current !== null && unownedRounds.includes(current)) return current;
  return hasNoHypotheses ? (unownedRounds[0] ?? null) : null;
}
function reconcileHypothesisDetail(
  current: HypothesisDetail | null,
  entries: HypothesisEntry[],
): HypothesisDetail | null {
  const entry =
    current === null
      ? undefined
      : entries.find((candidate, index) => entryKey(candidate, index) === current.entryKey);
  if (current === null || entry === undefined) return null;
  const rounds = scopeRounds(entry);
  return {
    entryKey: current.entryKey,
    selectedRound:
      current.selectedRound !== null && rounds.includes(current.selectedRound)
        ? current.selectedRound
        : (rounds.at(-1) ?? null),
  };
}

export function mergeExperimentEntries(
  current: readonly HypothesisEntry[],
  replacements: readonly HypothesisEntry[],
  removedIds: readonly string[],
): HypothesisEntry[] {
  const entries = new Map(current.map(entry => [entry.hypothesis_id, entry]));
  for (const entry of replacements) entries.set(entry.hypothesis_id, entry);
  for (const hypothesisId of removedIds) entries.delete(hypothesisId);
  return [...entries.values()];
}
export function failExperiments(state: SessionState, error: string): SessionState {
  return state.experimentLog === null
    ? state
    : {...state, experimentLog: {...state.experimentLog, pending: false, error}};
}
export function setDesignLog(state: SessionState, rounds: DesignRound[]): SessionState {
  return {...state, designLog: rounds};
}
export function designRoundFor(
  state: SessionState,
  roundNumber: number | null,
): DesignRound | null {
  return roundNumber === null || state.designLog === null
    ? null
    : (state.designLog.find(round => round.round === roundNumber) ?? null);
}
export function hypothesisRoundFor(
  state: SessionState,
  roundNumber: number | null,
): HypothesisRound | null {
  return experimentForRound(state.experimentLog?.entries ?? [], roundNumber)?.record ?? null;
}
export interface DesignRoundView {
  round: number;
  files: DesignFileChange[] | null;
  hypothesisId: string | null;
  title: string | null;
  record: HypothesisRound | null;
}
export function designRoundViews(
  designLog: readonly DesignRound[],
  entries: readonly HypothesisEntry[],
): DesignRoundView[] {
  return designLog.map(design => {
    const owner = experimentForRound(entries, design.round);
    return {
      round: design.round,
      files: design.files ?? null,
      hypothesisId: owner?.hypothesis.hypothesis_id ?? null,
      title: owner?.hypothesis.title ?? owner?.hypothesis.claim ?? null,
      record: owner?.record ?? null,
    };
  });
}
export function moveExperimentSelection(state: SessionState, delta: number): SessionState {
  const log = state.experimentLog;
  if (log === null || state.hypothesisDetail !== null || state.hypothesisScope !== null)
    return state;
  const items = experimentIndexItems(state);
  if (items.length === 0) return state;
  const selected = selectedExperimentIndexItem(state);
  const current = selected === null ? -1 : items.findIndex(item => item.key === selected.key);
  return selectExperimentIndexItem(
    state,
    items[Math.min(items.length - 1, Math.max(0, current + delta))],
  );
}
export function openHypothesisDetail(state: SessionState, requestedKey?: string): SessionState {
  const log = state.experimentLog;
  if (log === null || state.hypothesisScope !== null) return state;
  const key = requestedKey ?? log.selectedId;
  if (key === null) return state;
  const entry = log.entries.find((candidate, index) => entryKey(candidate, index) === key);
  if (entry === undefined) return state;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
    experimentLog: {
      ...log,
      selectedId: key,
      selectedActivity: false,
      selectedActivityRound: null,
      selectedUnownedRound: null,
    },
    hypothesisDetail: {entryKey: key, selectedRound: scopeRounds(entry).at(-1) ?? null},
  };
}
export function detailedHypothesis(state: SessionState): HypothesisEntry | null {
  const detail = state.hypothesisDetail;
  if (detail === null) return null;
  return (
    (state.experimentLog?.entries ?? []).find(
      (entry, index) => entryKey(entry, index) === detail.entryKey,
    ) ?? null
  );
}
export function moveHypothesisRoundSelection(state: SessionState, delta: number): SessionState {
  const detail = state.hypothesisDetail;
  const entry = detailedHypothesis(state);
  if (detail === null || entry === null || state.hypothesisScope !== null) return state;
  const rounds = scopeRounds(entry);
  if (rounds.length === 0) return state;
  const current = detail.selectedRound === null ? -1 : rounds.indexOf(detail.selectedRound);
  return {
    ...state,
    hypothesisDetail: {
      ...detail,
      selectedRound: rounds[Math.min(rounds.length - 1, Math.max(0, current + delta))] ?? null,
    },
  };
}
export function leaveHypothesisDetail(state: SessionState): SessionState {
  return state.hypothesisDetail === null || state.hypothesisScope !== null
    ? state
    : {...state, hypothesisDetail: null};
}
export function selectExperimentActivity(state: SessionState): SessionState {
  const log = state.experimentLog;
  const activity = hypothesisPlanningActivity(state);
  if (
    log === null ||
    state.hypothesisDetail !== null ||
    state.hypothesisScope !== null ||
    activity === null
  )
    return state;
  return {
    ...state,
    experimentLog: {
      ...log,
      selectedActivity: true,
      selectedActivityRound: activity.roundNumber,
      selectedUnownedRound: null,
    },
  };
}
export function enterExperimentDrilldown(state: SessionState): SessionState {
  if (state.hypothesisDetail !== null)
    return state.hypothesisDetail.selectedRound === null
      ? state
      : (enterExperimentRound(state, state.hypothesisDetail.selectedRound) ?? state);
  const activityRound = selectedPlanningActivityRound(state);
  const selectedRound =
    activityRound ??
    state.experimentLog?.selectedUnownedRound ??
    (selectedExperiment(state) === null ? unownedExperimentRounds(state)[0] : undefined);
  if (selectedRound !== undefined && selectedRound !== null)
    return enterUnownedExperimentRound(state, selectedRound) ?? state;
  return selectedExperiment(state) === null || state.hypothesisScope !== null
    ? state
    : openHypothesisDetail(state);
}

function selectedPlanningActivityRound(state: SessionState): number | undefined {
  const activity = hypothesisPlanningActivity(state);
  const log = state.experimentLog;
  if (activity === null || log === null) return undefined;
  const onlyItem = log.entries.length === 0 && (log.selectedUnownedRound ?? null) === null;
  return log.selectedActivity === true || onlyItem ? activity.roundNumber : undefined;
}
export function leaveExperimentDrilldown(state: SessionState): SessionState {
  return state.hypothesisScope === null
    ? state
    : {...state, hypothesisScope: null, selectedRound: null, selectedAgentKind: null};
}
export function scopeStateForRound(
  state: SessionState,
  roundNumber: number,
): Pick<SessionState, 'hypothesisScope' | 'hypothesisDetail' | 'experimentLog'> {
  const entries = state.experimentLog?.entries ?? [];
  const index = entries.findIndex(candidate => scopeRounds(candidate).includes(roundNumber));
  const entry = entries[index];
  if (entry === undefined)
    return {
      hypothesisScope: reuseScope(state.hypothesisScope, {
        id: `round-${roundNumber}`,
        label: `Round ${roundNumber}`,
        title: `Round ${roundNumber}`,
        rounds: [roundNumber],
        source: 'round',
      }),
      hypothesisDetail: null,
      experimentLog: state.experimentLog,
    };
  const key = entryKey(entry, index);
  return {
    hypothesisScope: reuseScope(state.hypothesisScope, {
      id: entry.hypothesis_id,
      label: hypothesisLabel(entry),
      title: hypothesisTitle(entry),
      rounds: scopeRounds(entry),
      source: 'hypothesis',
    }),
    hypothesisDetail: {entryKey: key, selectedRound: roundNumber},
    experimentLog:
      state.experimentLog === null || state.experimentLog.selectedId === key
        ? state.experimentLog
        : {...state.experimentLog, selectedId: key},
  };
}
function reuseScope(current: HypothesisScope | null, derived: HypothesisScope): HypothesisScope {
  return current !== null &&
    current.id === derived.id &&
    current.label === derived.label &&
    current.title === derived.title &&
    current.source === derived.source &&
    current.rounds.length === derived.rounds.length &&
    current.rounds.every((round, index) => round === derived.rounds[index])
    ? current
    : derived;
}
export function enterExperimentRound(
  state: SessionState,
  roundNumber: number,
): SessionState | null {
  if (!(state.experimentLog?.entries ?? []).some(entry => scopeRounds(entry).includes(roundNumber)))
    return null;
  return enterRound(state, roundNumber);
}
export function enterUnownedExperimentRound(
  state: SessionState,
  roundNumber: number,
): SessionState | null {
  return state.core.rounds.some(round => round.number === roundNumber)
    ? enterRound(state, roundNumber)
    : null;
}
function enterRound(state: SessionState, roundNumber: number): SessionState {
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
    ...scopeStateForRound(state, roundNumber),
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
  };
}
function entryKeyFor(entries: HypothesisEntry[], entry: HypothesisEntry): string | null {
  const index = entries.indexOf(entry);
  return index === -1 ? null : entryKey(entry, index);
}
function scopeRounds(entry: HypothesisEntry): number[] {
  return coreHypothesisRoundNumbers(entry);
}
export function hypothesisRoundNumbers(entry: HypothesisEntry): number[] {
  return coreHypothesisRoundNumbers(entry);
}
function hypothesisTitle(entry: HypothesisEntry): string {
  return entry.title ?? entry.hypothesis_id;
}
function hypothesisLabel(entry: HypothesisEntry): string {
  const range =
    entry.first_round === entry.last_round
      ? `r${entry.first_round}`
      : `r${entry.first_round}-${entry.last_round}`;
  return `${hypothesisTitle(entry)} · ${range}`;
}
function selectedExperiment(state: SessionState): HypothesisEntry | null {
  const log = state.experimentLog;
  if (log === null || log.selectedId === null) return null;
  const index = log.entries.map(entryKey).indexOf(log.selectedId);
  return index === -1 ? null : (log.entries[index] ?? null);
}
export function unownedExperimentRounds(
  state: SessionState,
  entries: HypothesisEntry[] = state.experimentLog?.entries ?? [],
): number[] {
  const owned = new Set(entries.flatMap(scopeRounds));
  const planningRound = hypothesisPlanningActivity(state)?.roundNumber;
  return state.core.rounds
    .flatMap(round =>
      round.number !== null &&
      !owned.has(round.number) &&
      round.number !== planningRound &&
      round.status !== 'planned'
        ? [round.number]
        : [],
    )
    .sort((left, right) => left - right);
}
export function experimentIndexItems(state: SessionState): ExperimentIndexItem[] {
  const history: ExperimentIndexItem[] = [];
  for (const [index, entry] of (state.experimentLog?.entries ?? []).entries())
    history.push({kind: 'hypothesis', key: entryKey(entry, index), entry});
  for (const roundNumber of unownedExperimentRounds(state))
    history.push({kind: 'round', key: `round-${roundNumber}`, roundNumber});
  history.sort((left, right) => experimentItemRound(left) - experimentItemRound(right));
  const activity = hypothesisPlanningActivity(state);
  return activity === null ? history : [...history, {kind: 'activity', key: 'activity', activity}];
}
function experimentItemRound(item: ExperimentIndexItem): number {
  return item.kind === 'hypothesis'
    ? item.entry.first_round
    : item.kind === 'round'
      ? item.roundNumber
      : item.activity.roundNumber;
}
function compareHypothesisEntries(left: HypothesisEntry, right: HypothesisEntry): number {
  return (
    left.first_round - right.first_round ||
    left.last_round - right.last_round ||
    left.hypothesis_id.localeCompare(right.hypothesis_id)
  );
}
export function selectedExperimentIndexItem(state: SessionState): ExperimentIndexItem | null {
  const log = state.experimentLog;
  if (log === null) return null;
  const items = experimentIndexItems(state);
  if (log.selectedActivity === true) return items.find(item => item.kind === 'activity') ?? null;
  if (log.selectedUnownedRound !== undefined && log.selectedUnownedRound !== null)
    return (
      items.find(item => item.kind === 'round' && item.roundNumber === log.selectedUnownedRound) ??
      null
    );
  return log.selectedId === null
    ? null
    : (items.find(item => item.kind === 'hypothesis' && item.key === log.selectedId) ?? null);
}
function selectExperimentIndexItem(
  state: SessionState,
  item: ExperimentIndexItem | undefined,
): SessionState {
  const log = state.experimentLog;
  if (log === null || item === undefined) return state;
  if (item.kind === 'activity')
    return {
      ...state,
      experimentLog: {
        ...log,
        selectedActivity: true,
        selectedActivityRound: item.activity.roundNumber,
        selectedUnownedRound: null,
      },
    };
  if (item.kind === 'round')
    return {
      ...state,
      experimentLog: {
        ...log,
        selectedActivity: false,
        selectedActivityRound: null,
        selectedUnownedRound: item.roundNumber,
      },
    };
  return {
    ...state,
    experimentLog: {
      ...log,
      selectedId: item.key,
      selectedActivity: false,
      selectedActivityRound: null,
      selectedUnownedRound: null,
    },
  };
}
export function hypothesisPlanningActivity(state: SessionState): HypothesisPlanningActivity | null {
  if (hasRunEnded(state.core) || state.experimentLog === null) return null;
  const phase = [...state.core.phases]
    .reverse()
    .find(candidate => candidate.status === 'active' && planningStageForPhase(candidate) !== null);
  if (phase === undefined || phase.roundNumber === null) return null;
  const roundNumber = phase.roundNumber;
  if (state.experimentLog.entries.some(entry => scopeRounds(entry).includes(roundNumber)))
    return null;
  const stage = planningStageForPhase(phase);
  if (stage === null) return null;
  const startedAt = earliestPlanningStartedAt(state.core.phases, roundNumber);
  return {stage, roundNumber, ...(startedAt === undefined ? {} : {startedAt})};
}
function earliestPlanningStartedAt(
  phases: readonly AgentPhase[],
  roundNumber: number,
): string | undefined {
  const starts = phases
    .filter(phase => phase.roundNumber === roundNumber && planningStageForPhase(phase) !== null)
    .flatMap(phase =>
      phase.startedAt === undefined
        ? []
        : [[phase.startedAt, Date.parse(phase.startedAt)] as const],
    )
    .filter(([, timestamp]) => Number.isFinite(timestamp));
  return starts.length === 0
    ? undefined
    : starts.reduce((earliest, candidate) =>
        candidate[1] < earliest[1] ? candidate : earliest,
      )[0];
}
/**
 * Rows are keyed by hypothesis identity rather than response position, so
 * selection survives refreshes even when the backend returns a different
 * order. The canonical index sorts the rows before presenting them.
 */
export function entryKey(entry: HypothesisEntry, index = 0): string {
  return entry.identified === false
    ? `${entry.hypothesis_id}#${entry.first_round}`
    : entry.hypothesis_id || `#${index}`;
}
