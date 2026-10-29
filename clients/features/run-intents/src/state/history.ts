import type {IntentOutcome} from '@vibesys/runs/intents';
import {IntentAction, IntentStep} from '@vibesys/runs/intents';

export interface OutcomeRow {
  readonly id: string;
  readonly source: 'intent' | 'epoch-started';
  readonly action: IntentAction;
  readonly status: 'applied' | 'refused';
  readonly reason: string | null;
  readonly epoch: bigint | null;
  readonly sequence: bigint | null;
}

function key(row: Pick<OutcomeRow, 'source' | 'id'>): string {
  return `${row.source}:${row.id}`;
}

function position(a: OutcomeRow, b: OutcomeRow): number {
  for (const key of ['epoch', 'sequence'] as const) {
    const left = a[key];
    const right = b[key];
    if (left === right) continue;
    if (left === null) return -1;
    if (right === null) return 1;
    return left < right ? -1 : 1;
  }
  return key(a) === key(b) ? 0 : key(a) < key(b) ? -1 : 1;
}

/** Writer positions, rather than arrival order, make replay and reconnect idempotent. */
export function foldOutcomes(
  previous: readonly OutcomeRow[],
  outcomes: readonly IntentOutcome[],
): readonly OutcomeRow[] {
  const rows = new Map(previous.map(row => [key(row), row]));
  for (const outcome of outcomes) {
    if (outcome.step !== IntentStep.APPLIED && outcome.step !== IntentStep.REFUSED) continue;
    const next: OutcomeRow = {
      id: outcome.controlId,
      source: outcome.source ?? 'intent',
      action: outcome.action,
      status: outcome.step === IntentStep.APPLIED ? 'applied' : 'refused',
      reason: outcome.reason ?? null,
      epoch: outcome.epoch ?? null,
      sequence: outcome.sequence ?? null,
    };
    const old = rows.get(key(next));
    if (!old || position(next, old) >= 0) rows.set(key(next), next);
  }
  const history = [...rows.values()];
  return history
    .filter(
      row =>
        row.source !== 'epoch-started' ||
        !history.some(
          other =>
            other.source === 'intent' &&
            other.action === IntentAction.RESUME &&
            other.status === 'applied' &&
            other.epoch === row.epoch,
        ),
    )
    .sort(position);
}
