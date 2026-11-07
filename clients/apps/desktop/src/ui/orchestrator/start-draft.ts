/** The Start run form's fields as the person typed them, and the StartRun command they become. */
import {Harness} from '@vibesys/orchestrator-proto';
import {type ModelChoices, startModel} from './start-models.js';

/**
 * The lead tools a run can start on, from the Harness enum itself: the key is the value's name in
 * lower case, as the command line and the agents' tools spell it, and what the form's choice holds.
 */
export type LeadChoice = Exclude<Lowercase<keyof typeof Harness>, 'unspecified'>;
export const LEADS = Object.fromEntries(
  (Object.keys(Harness) as (keyof typeof Harness)[])
    .filter(name => Number.isNaN(Number(name)) && name !== 'UNSPECIFIED')
    .map(name => [name.toLowerCase(), Harness[name]]),
) as Record<LeadChoice, Harness>;
export const LEAD_CHOICES = Object.keys(LEADS) as readonly LeadChoice[];

/** How many agents a run may have at once, at most: the daemon's own ceiling. */
export const FANOUT_CHOICES = [1, 2, 3, 4, 5] as const;
export const DEFAULT_FANOUT = 3;

/** The time limits a run can start with, in minutes; null is none. */
export const TIME_LIMITS = [null, 30, 60, 120, 240, 480, 1440] as const;
type TimeLimit = (typeof TIME_LIMITS)[number];

/**
 * The daemon requires some brief, and the brief is steering on top of the task the lead already
 * gets. A run started with no steering says so in these words.
 */
export const NO_STEERING = 'No further steering.';

export interface StartDraft {
  readonly repository: string;
  readonly task: string;
  readonly fanout: number;
  readonly lead: LeadChoice;
  /**
   * The model chosen for the lead, or the empty string for the form's own: the first model a host that
   * allows only some lists, else the tool's default. A choice the host's models do not offer is that too.
   */
  readonly model: string;
  /** The run's time limit in minutes, null for none. */
  readonly timeLimit: TimeLimit;
  /** The run's cost limit in dollars as typed, empty for none. */
  readonly costLimit: string;
  /** Optional steering for the lead, sent as the command's brief. */
  readonly steering: string;
}

export const emptyDraft: StartDraft = {
  repository: '',
  task: '',
  fanout: DEFAULT_FANOUT,
  lead: 'claude_code',
  model: '',
  timeLimit: null,
  costLimit: '',
  steering: '',
};

/** The fields of the StartRun command; the typed client checks the whole of it before it is sent. */
export interface StartRunRequest {
  readonly repository: string;
  readonly task: string;
  readonly fanoutLimit: number;
  readonly lead: {readonly harness: Harness; readonly model: string};
  readonly brief: string;
  /** How long the run may take, in seconds; null for no limit. */
  readonly maxTimeSeconds: number | null;
  /** The most the run may cost in dollars; null for no limit. */
  readonly maxCostUsd: number | null;
}

/** The cost limit as a number, null for none and NaN for text that is not a positive amount. */
function costOf(typed: string): number | null {
  const text = typed.trim();
  if (text === '') return null;
  const amount = Number(text);
  return Number.isFinite(amount) && amount > 0 ? amount : Number.NaN;
}

/** Why the draft cannot be started yet, or null when it can. */
export function missingFrom(draft: StartDraft, models: ModelChoices): string | null {
  if (draft.repository.trim() === '') return 'Name the repository to optimize.';
  if (draft.task.trim() === '') return 'Name the task to run.';
  if (models.blocked !== null) return models.blocked;
  if (Number.isNaN(costOf(draft.costLimit)))
    return 'The cost limit is a positive number of dollars.';
  return null;
}

/** The command for a draft that `missingFrom` accepts; surrounding spaces in a field are dropped. */
export function startRunRequest(draft: StartDraft, models: ModelChoices): StartRunRequest {
  const steering = draft.steering.trim();
  return {
    repository: draft.repository.trim(),
    task: draft.task.trim(),
    fanoutLimit: draft.fanout,
    lead: {harness: LEADS[draft.lead], model: startModel(draft.model, models)},
    brief: steering === '' ? NO_STEERING : steering,
    maxTimeSeconds: draft.timeLimit === null ? null : draft.timeLimit * 60,
    maxCostUsd: costOf(draft.costLimit),
  };
}
