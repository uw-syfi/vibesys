import type {SteerDeliveryFact} from '@vibesys/run-state';
import {
  IntentAction,
  type IntentState,
  IntentStep,
  SteerMode,
  type Record as StoreRecord,
  turnRole,
} from '@vibesys/runs/chat';

/**
 * Where each steer is, folded from the run's own records: what the run did with a request
 * (IntentState) and which turn received it (a `steer` link on the turn's span). The fold is
 * idempotent and independent of arrival order, so replay, a reconnect and a late record agree.
 */
export interface SteerFold {
  /** The highest sequence of any record seen; a reconnect resumes after it. */
  readonly throughSeq: bigint;
  readonly intents: ReadonlyMap<string, {readonly seq: bigint; readonly intent: IntentState}>;
  readonly links: ReadonlyMap<string, {readonly seq: bigint; readonly role: string | null}>;
}

export const emptySteerFold: SteerFold = {
  throughSeq: 0n,
  intents: new Map(),
  links: new Map(),
};

const STEER_LINK = 'steer';

type Intents = Map<string, {seq: bigint; intent: IntentState}>;
type Links = Map<string, {seq: bigint; role: string | null}>;

function noteIntent(intents: Intents, seq: bigint, intent: IntentState): void {
  const known = intents.get(intent.controlId);
  if (known === undefined || seq >= known.seq) intents.set(intent.controlId, {seq, intent});
}

function noteLinks(links: Links, record: StoreRecord): void {
  const body = record.body;
  if (body.case !== 'span') return;
  const label = body.value.span?.label;
  for (const link of label?.links ?? []) {
    const known = links.get(link.to);
    if (link.rel === STEER_LINK && (known === undefined || record.seq < known.seq))
      links.set(link.to, {seq: record.seq, role: turnRole(label?.attrs)});
  }
}

export function foldSteer(fold: SteerFold, records: readonly StoreRecord[]): SteerFold {
  let throughSeq = fold.throughSeq;
  const intents: Intents = new Map(fold.intents);
  const links: Links = new Map(fold.links);
  for (const record of records) {
    if (record.seq > throughSeq) throughSeq = record.seq;
    if (record.body.case === 'intent') noteIntent(intents, record.seq, record.body.value);
    else noteLinks(links, record);
  }
  return {throughSeq, intents, links};
}

/** The step a recorded outcome is; the run's reason code tells a drop and an ending from a refusal. */
function stepOf(intent: IntentState): SteerDeliveryFact['step'] {
  if (intent.action === IntentAction.WITHDRAW) {
    return intent.step === IntentStep.REFUSED ? 'refused' : 'ended';
  }
  if (intent.reasonCode === 'dropped') return 'dropped';
  if (intent.reasonCode === 'ended') return 'ended';
  return intent.step === IntentStep.REFUSED ? 'refused' : 'queued';
}

function intentFact(seq: bigint, intent: IntentState): SteerDeliveryFact | null {
  const withdraw = intent.action === IntentAction.WITHDRAW;
  if (!withdraw && intent.action !== IntentAction.STEER) return null;
  const step = stepOf(intent);
  return {
    sequence: Number(seq),
    step,
    controlId: intent.controlId,
    action: withdraw ? 'withdraw' : 'steer',
    withdraws: intent.withdraws ?? null,
    role: null,
    attemptId: null,
    reason:
      withdraw && step === 'ended' ? 'withdrawn' : (intent.reason ?? intent.reasonCode ?? null),
  };
}

/** The delivery steps the fold stands for, in journal order. */
export function steerDeliveries(fold: SteerFold): readonly SteerDeliveryFact[] {
  const facts: SteerDeliveryFact[] = [];
  for (const {seq, intent} of fold.intents.values()) {
    const fact = intentFact(seq, intent);
    if (fact !== null) facts.push(fact);
  }
  for (const [controlId, link] of fold.links) {
    const standing = fold.intents.get(controlId)?.intent.mode === SteerMode.STANDING;
    facts.push({
      sequence: Number(link.seq),
      step: standing ? 'in_force' : 'reached',
      controlId,
      action: 'steer',
      withdraws: null,
      role: link.role,
      attemptId: null,
      reason: null,
    });
  }
  return facts.sort((left, right) => left.sequence - right.sequence);
}
