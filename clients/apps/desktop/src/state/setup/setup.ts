/**
 * A run's setup as the screens read it: the draft task, its six checks, the baseline and what setup
 * has cost, from the `SetupView` the host sends whole in each run's summary. Plain selectors over the
 * fold's state. The host derives every fact here (checks come from the jobs that ran, never from an
 * agent's account) and decides whether setup can complete; this reads the view into rows that never
 * change in number or order, so a screen's rows do not move as facts arrive.
 */
import {
  type Agent,
  type Receipt,
  Refusal,
  type Run,
  RunPhase,
  SetupCheckId,
  SetupCheckState,
  type SetupView,
  type TaskCard,
} from '@vibesys/orchestrator-proto';
import type {CommandProgress} from '../commands.js';
import type {OrchestratorState} from '../orchestrator/state.js';

/** The six checks, in the order the host sends and a screen draws them. */
export const SETUP_CHECKS: readonly SetupCheckId[] = [
  SetupCheckId.OBJECTIVE,
  SetupCheckId.CORRECTNESS_CHECK,
  SetupCheckId.BENCHMARK,
  SetupCheckId.BASE_PASSES,
  SetupCheckId.BROKEN_FAILS,
  SetupCheckId.NUMBER_STABLE,
];

export type CheckState = 'pending' | 'running' | 'passed' | 'failed';

const STATES: Readonly<Record<SetupCheckState, CheckState>> = {
  [SetupCheckState.UNSPECIFIED]: 'pending',
  [SetupCheckState.PENDING]: 'pending',
  [SetupCheckState.RUNNING]: 'running',
  [SetupCheckState.PASSED]: 'passed',
  [SetupCheckState.FAILED]: 'failed',
};

export interface CheckRow {
  readonly id: SetupCheckId;
  readonly state: CheckState;
  /** The host's short account of the state, or empty. */
  readonly detail: string;
}

/** What the draft task is: a card the host read, the reader's words for why it has none, or nothing yet. */
export type Draft =
  | {readonly kind: 'card'; readonly card: TaskCard}
  | {readonly kind: 'unreadable'; readonly detail: string}
  | {readonly kind: 'none'};

export interface Baseline {
  readonly value: number;
  readonly unit: string;
  readonly spreadPercent: number | null;
}

export interface SetupFacts {
  /** The task as declared; empty until the setup agent names it. */
  readonly task: string;
  readonly draft: Draft;
  /** One row per check, in `SETUP_CHECKS` order, whatever the host sent. */
  readonly checks: readonly CheckRow[];
  /** The baseline measurement, once one exists. */
  readonly baseline: Baseline | null;
  /** What setup has cost so far, apart from the run's limits. */
  readonly spentUsd: number;
  /** How long setup has lasted, in whole seconds, as the host last said. */
  readonly elapsedSeconds: number;
  /** Why the host could not read the facts just now ("as of the last read"); null when it could. */
  readonly readFailure: string | null;
  /** Every check passed. The host decides again when Start is pressed. */
  readonly startable: boolean;
}

function draftOf(view: SetupView): Draft {
  const {draft} = view;
  switch (draft.case) {
    case 'card':
      return {kind: 'card', card: draft.value};
    case 'unreadable':
      return {kind: 'unreadable', detail: draft.value.detail};
    case undefined:
      return {kind: 'none'};
    default:
      draft satisfies never;
      return {kind: 'none'};
  }
}

/** The checks as six rows in id order: a check the host did not send is pending, never missing. */
function rowsOf(view: SetupView): readonly CheckRow[] {
  return SETUP_CHECKS.map(id => {
    const sent = view.checks.find(check => check.id === id);
    return {
      id,
      state: sent === undefined ? 'pending' : STATES[sent.state],
      detail: sent?.detail ?? '',
    };
  });
}

/** One setup view as the screens read it. */
export function setupFacts(view: SetupView): SetupFacts {
  const checks = rowsOf(view);
  return {
    task: view.task,
    draft: draftOf(view),
    checks,
    baseline:
      view.baselineValue === undefined
        ? null
        : {
            value: view.baselineValue,
            unit: view.baselineUnit,
            spreadPercent: view.baselineSpreadPercent ?? null,
          },
    spentUsd: view.spentUsd,
    elapsedSeconds: Number(view.elapsed?.seconds ?? 0n),
    readFailure: view.readFailure === '' ? null : view.readFailure,
    startable: checks.every(check => check.state === 'passed'),
  };
}

/** What a run in setup shows before the host has sent its summary: six pending checks and nothing known. */
export const UNREAD_SETUP: SetupFacts = {
  task: '',
  draft: {kind: 'none'},
  checks: SETUP_CHECKS.map(id => ({id, state: 'pending', detail: ''})),
  baseline: null,
  spentUsd: 0,
  elapsedSeconds: 0,
  readFailure: null,
  startable: false,
};

/** The setup of a run as its last summary says, or undefined for a run with none (never in setup, or no summary yet). */
export function setupOf(state: OrchestratorState, runId: string): SetupFacts | undefined {
  const view = state.summaries[runId]?.setup;
  return view === undefined ? undefined : setupFacts(view);
}

/** Whether the run is in its setup phase: a run recorded before phases is optimizing. */
export const inSetup = (run: Run): boolean => run.phase === RunPhase.SETUP;

/** The agent the person chats with in a run's setup: its lead. */
export function setupLead(state: OrchestratorState, runId: string): Agent | undefined {
  const run = state.runs[runId];
  return run === undefined ? undefined : state.agents[run.leadAgentId];
}

/**
 * The agent a screen of a run follows: the one the route opened, else, for a run in setup, its lead,
 * whose conversation is the run's page.
 */
export function followedAgent(
  state: OrchestratorState,
  runId: string | undefined,
  opened: string | null | undefined,
): string | null {
  if (opened !== null && opened !== undefined) return opened;
  const run = runId === undefined ? undefined : state.runs[runId];
  return run !== undefined && inSetup(run) && run.leadAgentId !== '' ? run.leadAgentId : null;
}

/** The checks the host named as unmet in its refusal of Start, in the order of the rows. */
export function unmetOf(receipt: Receipt): readonly SetupCheckId[] {
  return SETUP_CHECKS.filter(id => receipt.unmetChecks.includes(id));
}

/** Where the last press of Start stands, as the setup screen shows it. */
export type StartOutcome =
  | {readonly kind: 'none'}
  | {readonly kind: 'sending'}
  /** The host took it: the run is optimizing as soon as its summary says so. */
  | {readonly kind: 'taken'}
  /** The host refused because these checks have not passed (as its own derivation reads them). */
  | {readonly kind: 'incomplete'; readonly unmet: readonly SetupCheckId[]}
  | {readonly kind: 'refused'; readonly receipt: Receipt}
  /** The outcome is not known or the command never went. */
  | {readonly kind: 'unsent'; readonly progress: CommandProgress};

/** What the last CompleteSetup of the run came to, from its progress. */
export function startOutcome(progress: CommandProgress | undefined): StartOutcome {
  if (progress === undefined) return {kind: 'none'};
  switch (progress._tag) {
    case 'Sending':
      return {kind: 'sending'};
    case 'Accepted':
      return {kind: 'taken'};
    case 'Refused':
      return progress.receipt.refusal === Refusal.SETUP_INCOMPLETE
        ? {kind: 'incomplete', unmet: unmetOf(progress.receipt)}
        : {kind: 'refused', receipt: progress.receipt};
    default:
      return {kind: 'unsent', progress};
  }
}
