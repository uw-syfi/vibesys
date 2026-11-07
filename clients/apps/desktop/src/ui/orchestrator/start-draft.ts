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
  /** The eval place of a run in the person's own repository: the host's name for it, empty to let the host resolve. */
  readonly evalPlace: string;
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
  evalPlace: '',
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
  /** The eval place, empty to let the host resolve it. */
  readonly evalPlace: string;
}

/**
 * Dollars as a person writes them: digits with an optional decimal point, `$` before them allowed (the
 * field's label names the unit). No exponent, no other base, and no commas: a comma is a thousands
 * separator in one place and the decimal point in another, so `1,000` is refused rather than guessed.
 */
const DOLLARS = /^(-?)\s*\$?\s*(\d+(?:\.\d*)?|\.\d+)$/;

/** The cost limit typed: none when empty, else the dollars it reads as, or why it reads as none. */
function readCost(typed: string): {readonly usd: number | null} | {readonly wrong: string} {
  const text = typed.trim();
  if (text === '') return {usd: null};
  const found = DOLLARS.exec(text);
  if (found === null) {
    return {
      wrong: text.includes(',')
        ? 'The cost limit is written without commas, such as 1000 or 12.50.'
        : 'The cost limit is a number of dollars, such as 25 or 12.50.',
    };
  }
  const usd = Number(found[2]);
  if (found[1] === '-' || usd === 0) {
    return {wrong: 'The cost limit must be more than zero. Leave it empty for no limit.'};
  }
  return Number.isFinite(usd) ? {usd} : {wrong: 'The cost limit is too large.'};
}

/** The command for a draft, regardless of whether it may be sent; surrounding spaces are dropped. */
export function startRunRequest(draft: StartDraft, models: ModelChoices): StartRunRequest {
  const steering = draft.steering.trim();
  const cost = readCost(draft.costLimit);
  return {
    repository: draft.repository.trim(),
    task: draft.task.trim(),
    fanoutLimit: draft.fanout,
    lead: {harness: LEADS[draft.lead], model: startModel(draft.model, models)},
    brief: steering === '' ? NO_STEERING : steering,
    maxTimeSeconds: draft.timeLimit === null ? null : draft.timeLimit * 60,
    maxCostUsd: 'usd' in cost ? cost.usd : null,
    evalPlace: draft.evalPlace,
  };
}

/** The fields of the form that can hold something a start cannot take. */
export type ProblemField = 'repository' | 'task' | 'model' | 'costLimit';

/** What keeps a draft from starting, and the field it is about. */
export interface DraftProblem {
  readonly field: ProblemField;
  readonly message: string;
}

/**
 * A draft read whole: the command it starts, or what keeps it from starting, in the form's order. No
 * command is made from a draft with a problem, so a field that reads as nothing is never sent as none.
 */
export type DraftCheck =
  | {readonly _tag: 'Ready'; readonly request: StartRunRequest}
  | {readonly _tag: 'NotReady'; readonly problems: readonly [DraftProblem, ...DraftProblem[]]};

/**
 * Reads the draft for the host's model choices; surrounding spaces in a field are dropped.
 * `example`: the repository and task come from the catalogue entry, not from the fields, so neither
 * is checked here.
 */
export function checkDraft(
  draft: StartDraft,
  models: ModelChoices,
  source: 'repository' | 'example' = 'repository',
): DraftCheck {
  const cost = readCost(draft.costLimit);
  const problems = [
    source === 'repository' && draft.repository.trim() === ''
      ? {field: 'repository', message: 'Name the repository to optimize.'}
      : null,
    source === 'repository' && draft.task.trim() === ''
      ? {field: 'task', message: 'Name the task to run.'}
      : null,
    models.blocked === null ? null : {field: 'model', message: models.blocked},
    'wrong' in cost ? {field: 'costLimit', message: cost.wrong} : null,
  ].filter((problem): problem is DraftProblem => problem !== null);
  const [first, ...rest] = problems;
  if (first !== undefined) return {_tag: 'NotReady', problems: [first, ...rest]};
  return {_tag: 'Ready', request: startRunRequest(draft, models)};
}

/**
 * Why the draft cannot be started yet, or null when it can: `checkDraft`'s first problem, by message
 * rather than by field, for a caller that names the field no other way (the setup chat's own button).
 */
export function missingFrom(
  draft: StartDraft,
  models: ModelChoices,
  source: 'repository' | 'example' = 'repository',
): string | null {
  const check = checkDraft(draft, models, source);
  return check._tag === 'Ready' ? null : check.problems[0].message;
}
