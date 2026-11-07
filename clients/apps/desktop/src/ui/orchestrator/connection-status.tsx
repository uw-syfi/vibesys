import {Button, Chip, Row, type StatusTone} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type Fault,
  type FeedStatus,
  type HostConnectionState,
  type HostStatusKind,
  hostStatusOf,
} from '../../state/index.js';
import {hostStatusWord} from '../host-status-words.js';
import {faultHint} from './words.js';

/** One thing about a connection a person should know, with the way to try again. */
export interface ConnectionNote {
  readonly key: string;
  /** What the note is about: a host's runs, or one agent's steps on it. */
  readonly what: string;
  readonly connection: HostConnectionState;
  readonly feed: FeedStatus;
  /** The screen already shows something read earlier, so the note says it may be out of date. */
  readonly hasData: boolean;
  /** How long what is shown has been out of date, from the host's freshness; null while live. */
  readonly staleForMs: number | null;
  readonly tryAgain: () => void;
}

/** One chip of a host's status: its one word, and the account behind it for a hover. */
interface Say {
  readonly label: string;
  readonly tone: StatusTone;
  /** The account behind the label, never a line of the screen: its context, the fault's advice, its details. */
  readonly hint: string;
  readonly canRetry: boolean;
}

const hintOf = (fault: Fault, context: string): string =>
  [faultHint(fault), context, fault.detail === '' ? '' : `Details: ${fault.detail}`]
    .filter(part => part !== '')
    .join(' ');

/** The fault behind a status: the link's own, else the feed's. */
function faultBehind({connection, feed}: ConnectionNote): Fault | null {
  if (connection._tag === 'Unusable' || connection._tag === 'Waiting') return connection.fault;
  if (connection._tag === 'Ready' && feed._tag === 'Failed') return feed.fault;
  return null;
}

/** The statuses a press of Retry can change: the host is not delivering and will not unaided. */
const RETRYABLE: ReadonlySet<HostStatusKind> = new Set([
  'stale',
  'unavailable',
  'unreachable',
  'incompatible',
  'refused',
]);

/**
 * What the host's one status (`hostStatusOf`, the same word every other screen shows) says as a
 * chip, or null when it is working and nothing needs saying. This derives nothing of its own: the
 * word, its tone and its age are the status's; the note only adds the account behind them.
 */
function sayAbout(note: ConnectionNote): Say | null {
  const {connection, feed, hasData, what} = note;
  const status = hostStatusOf({connection, feed, hasData, staleForMs: note.staleForMs});
  if (status.kind === 'live') return null;
  const {label, tone, hint} = hostStatusWord(status);
  const reading = hasData ? 'What is shown may be out of date.' : `Nothing read from ${what} yet.`;
  const fault = faultBehind(note);
  const context =
    status.kind === 'catching-up'
      ? `Catching up with ${what}.`
      : status.kind === 'connecting' || status.kind === 'waiting'
        ? `Connecting to read ${what}.`
        : status.kind === 'incompatible'
          ? incompatibleHint(connection)
          : reading;
  return {
    label,
    tone,
    hint: hint ?? (fault === null ? context : hintOf(fault, context)),
    canRetry: RETRYABLE.has(status.kind),
  };
}

function incompatibleHint(connection: HostConnectionState): string {
  const fix =
    connection._tag === 'Incompatible' && connection.side === 'client-too-old'
      ? 'Update this app.'
      : 'Update VibeSys on that host.';
  return `This app and the VibeSys on the host cannot work together. ${fix}`;
}

/**
 * The state of every connection that is not working, in the header of every screen: chips (connecting,
 * catching up, or lost with its reason and how stale what is shown is) and one Retry. The longer
 * account of each is its hover and focus. A working connection shows nothing, and no state is a banner.
 */
export function ConnectionStatus(props: {
  readonly notes: readonly ConnectionNote[];
}): JSX.Element | null {
  const said = props.notes.flatMap(note => {
    const say = sayAbout(note);
    return say === null ? [] : [{note, say}];
  });
  // Two notes of one host say the same thing: it is said once.
  const shown = said.filter(
    ({say}, index) => said.findIndex(other => other.say.label === say.label) === index,
  );
  if (shown.length === 0) return null;
  const retry = shown.find(({say}) => say.canRetry);
  return (
    <div role="status" aria-live="polite" aria-label="Connection">
      <Row gap="space-2" wrap>
        {shown.map(({note, say}) => (
          <Row key={note.key} gap="space-2">
            <Chip label={say.label} tone={say.tone} dot hint={say.hint} />
          </Row>
        ))}
        {retry === undefined ? null : (
          <Button
            text="Retry"
            icon="refresh"
            label={`Retry connection to read ${retry.note.what}`}
            variant="quiet"
            size="sm"
            press={retry.note.tryAgain}
          />
        )}
      </Row>
    </div>
  );
}
