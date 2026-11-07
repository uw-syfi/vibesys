/**
 * What a host's one status is called, for every screen that shows it: Settings, the Runs list, the
 * run's Cost. A refusal says its cause as its label, with the cause's hint on hover (`refusal-words`
 * stays their source); every other status is one word, and a stale one says how long it has been.
 */
import type {StatusTone} from '@vibesys/ui';
import type {HostStatus, HostStatusKind} from '../state/index.js';
import {span} from './duration.js';
import {causeHint, causeWord} from './refusal-words.js';

const WORD: Readonly<Record<HostStatusKind, {label: string; tone: StatusTone}>> = {
  live: {label: 'Live', tone: 'live'},
  'catching-up': {label: 'Catching up', tone: 'neutral'},
  waiting: {label: 'Waiting', tone: 'neutral'},
  connecting: {label: 'Connecting', tone: 'neutral'},
  stale: {label: 'Stale', tone: 'warn'},
  unavailable: {label: 'Unavailable', tone: 'err'},
  unreachable: {label: 'Unreachable', tone: 'err'},
  incompatible: {label: 'Incompatible', tone: 'err'},
  refused: {label: 'Refused', tone: 'err'},
};

/** Every label a status can have apart from a cause's own: what no cause's label may be taken for. */
export const HOST_STATUS_LABELS: readonly string[] = Object.values(WORD).map(word => word.label);

/** A host stale for less than this has no age worth saying: it has only just lost its link. */
const AGE_WORTH_SAYING_MS = 10_000;

export interface HostStatusWord {
  readonly label: string;
  readonly tone: StatusTone;
  /** The longer account behind the label, for a hover, when there is one. */
  readonly hint?: string;
}

/** The one word of a host's status, its tone and, for a refusal with a cause, the way to fix it. */
export function hostStatusWord(status: HostStatus): HostStatusWord {
  const {label, tone} = WORD[status.kind];
  if (status.kind === 'refused' && status.cause !== null) {
    return {label: causeWord(status.cause), tone, hint: causeHint(status.cause)};
  }
  if (
    status.kind === 'stale' &&
    status.staleForMs !== null &&
    status.staleForMs >= AGE_WORTH_SAYING_MS
  ) {
    return {label: `${label} ${span(status.staleForMs)}`, tone};
  }
  return {label, tone};
}
