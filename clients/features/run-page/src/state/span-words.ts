/**
 * How a span reads in words: its title, state and verdict, and what it waits on. The kinds the
 * dynamic search declares (planning call, workstream, agent turn, gate, adoption) get their own
 * titles; any other kind, or a span with no label, reads plainly from its name and title. Nothing
 * is guessed from a span's name.
 */
import {roleName, type Span} from '@vibesys/contracts';
import {stepName, stringAttr, words} from './span-reading.js';
import type {Tone} from './views.js';

type Status = Span['status'];
type Waiting = NonNullable<Span['waiting_on']>[number];

const STATUS: Readonly<Record<Status, {readonly label: string; readonly tone: Tone}>> = {
  pending: {label: 'Not started', tone: 'neutral'},
  running: {label: 'Running', tone: 'live'},
  waiting: {label: 'Waiting', tone: 'warn'},
  done: {label: 'Done', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  stopped: {label: 'Stopped', tone: 'neutral'},
};

export function statusWords(status: Status): {readonly label: string; readonly tone: Tone} {
  return STATUS[status];
}

const VERDICTS: Readonly<Record<string, {readonly label: string; readonly tone: Tone}>> = {
  passed: {label: 'Passed', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  slower: {label: 'Slower than the input', tone: 'warn'},
  failed_check: {label: 'Failed a check', tone: 'err'},
  adopted: {label: 'Adopted', tone: 'ok'},
  not_adopted: {label: 'Not adopted', tone: 'warn'},
};

/** A recorded agent client, or null when the turn did not name one. */
export function clientWord(value: string | null): 'scripted' | 'provider' | null {
  if (value === null) return null;
  const word = value.replace(/^AGENT_CLIENT_/, '').toLowerCase();
  if (word === 'scripted' || word === 'provider') return word;
  return null;
}

/** The checkout skip the adoption recorded, as a status word. */
export function checkoutWords(value: string | null): string | null {
  if (value === null) return null;
  const word = value.replace(/^CHECKOUT_STATUS_/, '').toLowerCase();
  if (word === '' || word === 'unspecified') return null;
  return plain(word);
}

/** A word from the wire (`failed_check`) as text (`Failed check`). */
export function plain(word: string): string {
  const spaced = words(word);
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** The span's verdict in words, or null when it has none. */
export function verdictWords(span: Span): {readonly label: string; readonly tone: Tone} | null {
  const verdict = span.label?.verdict;
  if (verdict === null || verdict === undefined) return null;
  return VERDICTS[verdict] ?? {label: plain(verdict), tone: 'neutral'};
}

/** Whether the span has ended (it will not change state again). */
export function ended(span: Span): boolean {
  return span.status === 'done' || span.status === 'failed' || span.status === 'stopped';
}

/** How a gate's stages read in its title: a task's build stage is the candidate build. */
const GATE_STAGE_WORDS: Readonly<Record<string, string>> = {build: 'candidate build'};

/** A gate's title (its stages joined by ` + `, as the search labels it) in words. */
function gateTitle(title: string): string {
  return title
    .split(' + ')
    .map(stage => GATE_STAGE_WORDS[stage] ?? stage)
    .join(' + ');
}

/** The span's title on its row. */
export function titleOf(span: Span): string {
  const label = span.label;
  const title = label?.title ?? stepName(span.name) ?? span.name;
  if (span.parent === null) return `Run · ${title}`;
  switch (label?.kind) {
    case 'planning_call':
      return plain(title);
    case 'workstream': {
      const hypothesis = stringAttr(span, 'hypothesis_id');
      return hypothesis === null || hypothesis === title ? title : `${hypothesis} · ${title}`;
    }
    case 'agent_turn': {
      const role = roleName(title);
      if (clientWord(stringAttr(span, 'client')) === 'scripted') return role;
      const model = stringAttr(span, 'model');
      return model === null ? role : `${role} · ${model}`;
    }
    case 'gate':
      return `Gate · ${gateTitle(title)}`;
    case 'adoption': {
      const candidate = stringAttr(span, 'candidate');
      const base =
        candidate === null ? 'Adopt the input' : `Adopt candidate ${candidate.slice(0, 7)}`;
      const skip = checkoutWords(stringAttr(span, 'checkout_status'));
      return skip === null ? base : `${base} · ${skip}`;
    }
    default:
      return title;
  }
}

/** Adoption span id to the headline the records name, including a checkout skip. */
export function adoptionTitles(
  spans: ReadonlyMap<string, Span> | undefined,
): ReadonlyMap<string, string> {
  const titles = new Map<string, string>();
  if (spans === undefined) return titles;
  for (const span of spans.values()) {
    if (span.label?.kind !== 'adoption') continue;
    titles.set(span.id, titleOf(span));
  }
  return titles;
}

/**
 * What the span waits on that is worth saying. Every live flow also awaits the operator's stop, so
 * the run records a `stop` wait on each from the start; it is something the flow waits on only
 * once a stop was requested.
 */
export function openWaits(span: Span): readonly Waiting[] {
  if (ended(span)) return [];
  const requested = span.stop !== null && span.stop !== undefined;
  return (span.waiting_on ?? []).filter(wait => wait.kind !== 'stop' || requested);
}
