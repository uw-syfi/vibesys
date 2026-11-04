/** The Start run form's fields as the person typed them, and the StartRun command they become. */
import {Harness} from '@vibesys/orchestrator-proto';

/** The lead tools a run can start on; the key is what the form's choice holds. */
export const LEADS = {claude_code: Harness.CLAUDE_CODE, codex: Harness.CODEX} as const;
export type LeadChoice = keyof typeof LEADS;
export const LEAD_CHOICES = Object.keys(LEADS) as readonly LeadChoice[];

/** How many agents a run may have at once, at most: the daemon's own ceiling. */
export const FANOUT_CHOICES = [1, 2, 3, 4, 5] as const;
export const DEFAULT_FANOUT = 3;

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
  /** Optional steering for the lead, sent as the command's brief. */
  readonly steering: string;
}

export const emptyDraft: StartDraft = {
  repository: '',
  task: '',
  fanout: DEFAULT_FANOUT,
  lead: 'claude_code',
  steering: '',
};

/** The fields of the StartRun command; the typed client checks the whole of it before it is sent. */
export interface StartRunRequest {
  readonly repository: string;
  readonly task: string;
  readonly fanoutLimit: number;
  readonly lead: {readonly harness: Harness};
  readonly brief: string;
}

/** Why the draft cannot be started yet, or null when it can. */
export function missingFrom(draft: StartDraft): string | null {
  if (draft.repository.trim() === '') return 'Name the repository to optimize.';
  if (draft.task.trim() === '') return 'Name the task to run.';
  return null;
}

/** The command for a draft that `missingFrom` accepts; surrounding spaces in a field are dropped. */
export function startRunRequest(draft: StartDraft): StartRunRequest {
  const steering = draft.steering.trim();
  return {
    repository: draft.repository.trim(),
    task: draft.task.trim(),
    fanoutLimit: draft.fanout,
    lead: {harness: LEADS[draft.lead]},
    brief: steering === '' ? NO_STEERING : steering,
  };
}
