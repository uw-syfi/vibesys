/**
 * Pure selectors over the operator-intervention timeline (`FoldState.operatorActions`
 * / `CampaignViewModel.operatorActions`): the dwell-time indicator for a pause or
 * stop with no later resolution, and the steered-prompt join between a
 * `steer_consumed` entry and the agent execution it landed on.
 *
 * Both take their reference point as an explicit argument (`now`, or the list of
 * known executions) rather than reading a clock or a live subscription, so tests
 * drive them with fixed data and never sleep.
 */
import type {FrameOperatorAction, OperatorAction} from './campaign-frames.js';

/** Elapsed time past which a dwell is called out as overdue rather than merely
 * in progress. */
const DWELL_ESCALATION_MS = 60 * 60 * 1000;

export type DwellSeverity = 'normal' | 'escalated';

/** An operator action that leaves the run waiting until a later transition
 * resolves it: a pause (requested or landed) waits for a resume; a stop
 * request waits for the stop to land. */
export type WaitingAction = 'pause_requested' | 'paused' | 'stop_requested';

export interface DwellInfo {
  readonly action: WaitingAction;
  /** Timestamp of the transition that started the wait. */
  readonly since: string;
  readonly elapsedMs: number;
  readonly severity: DwellSeverity;
}

const WAITING_ACTIONS: readonly WaitingAction[] = ['pause_requested', 'paused', 'stop_requested'];
const RESOLVING_ACTIONS: readonly OperatorAction[] = ['resumed', 'stopped'];

function isWaitingAction(action: OperatorAction): action is WaitingAction {
  return (WAITING_ACTIONS as readonly OperatorAction[]).includes(action);
}

/**
 * The run's current operator-wait state: the most recent pause/stop
 * transition with no later `resumed`/`stopped` transition after it, or null
 * if the run is not currently waiting on an operator. `steer_queued` and
 * `steer_consumed` entries do not affect this: steering is independent of
 * the pause/stop wait.
 */
export function dwellTime(entries: readonly FrameOperatorAction[], now: number): DwellInfo | null {
  let waiting: FrameOperatorAction | null = null;
  for (const entry of entries) {
    if (isWaitingAction(entry.action)) waiting = entry;
    else if (RESOLVING_ACTIONS.includes(entry.action)) waiting = null;
  }
  if (waiting === null) return null;
  const since = waiting.timestamp;
  const elapsedMs = Math.max(0, now - Date.parse(since));
  return {
    action: waiting.action as WaitingAction,
    since,
    elapsedMs,
    severity: elapsedMs >= DWELL_ESCALATION_MS ? 'escalated' : 'normal',
  };
}

/** Formats an elapsed duration as e.g. "6h 12m" or "45m". */
export function formatDwell(elapsedMs: number): string {
  const totalMinutes = Math.floor(elapsedMs / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

const WAITING_LABEL: Record<WaitingAction, string> = {
  pause_requested: 'Pausing',
  paused: 'Paused',
  stop_requested: 'Stopping',
};

/** The dwell banner text, e.g. "Paused 6h 12m, waiting on an operator". */
export function dwellMessage(info: DwellInfo): string {
  return `${WAITING_LABEL[info.action]} ${formatDwell(info.elapsedMs)}, waiting on an operator`;
}

export interface ExecutionStarted {
  readonly executionId: string;
  readonly userPrompt: string;
}

/**
 * The steered-prompt tail for one `steer_consumed` timeline entry: the
 * `userPrompt` of the agent execution sharing its `executionId`, or null if
 * the entry carries no `executionId` or no matching execution is known.
 */
export function steeredPromptTail(
  entry: Pick<FrameOperatorAction, 'action' | 'executionId'>,
  executions: readonly ExecutionStarted[],
): string | null {
  if (entry.action !== 'steer_consumed' || entry.executionId === null) return null;
  const match = executions.find(execution => execution.executionId === entry.executionId);
  return match?.userPrompt ?? null;
}
