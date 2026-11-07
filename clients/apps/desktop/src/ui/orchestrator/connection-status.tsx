import {Button, Chip, Row} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type Fault,
  type FeedStatus,
  freshnessOf,
  type HostConnectionState,
} from '../../state/index.js';
import {faultHint, faultText, formatDuration} from './words.js';

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

type Tone = 'live' | 'warn';

/** One chip of the connection's state: a label, and the account behind it for a hover. */
interface Say {
  readonly label: string;
  readonly tone: Tone;
  /** The account behind the label, never a line of the screen: its context, the fault's advice, its details. */
  readonly hint: string;
  /** How long what is shown has been out of date, when that is worth saying. */
  readonly stale?: string;
  readonly canRetry: boolean;
}

const hintOf = (fault: Fault, context: string): string =>
  [faultHint(fault), context, fault.detail === '' ? '' : `Details: ${fault.detail}`]
    .filter(part => part !== '')
    .join(' ');

/** What the connection says as a chip, or null when it is working and nothing needs saying. */
function sayAbout(note: ConnectionNote): Say | null {
  const {connection, feed, hasData, what} = note;
  const reading = hasData ? 'What is shown may be out of date.' : `Nothing read from ${what} yet.`;
  if (connection._tag === 'Incompatible') {
    const fix =
      connection.side === 'client-too-old' ? 'Update this app.' : 'Update VibeSys on that host.';
    return {
      label: 'Incompatible',
      tone: 'warn',
      hint: `This app and the VibeSys on the host cannot work together. ${fix}`,
      canRetry: true,
    };
  }
  if (connection._tag === 'Unusable') {
    return {
      label: faultText(connection.fault),
      tone: 'warn',
      hint: hintOf(connection.fault, `Automatic retries stopped. ${reading}`),
      canRetry: true,
    };
  }
  if (connection._tag === 'Waiting') {
    return {
      label: faultText(connection.fault),
      tone: 'warn',
      hint: hintOf(connection.fault, `${reading} Trying again.`),
      ...(note.staleForMs === null || !hasData ? {} : {stale: formatDuration(note.staleForMs)}),
      canRetry: true,
    };
  }
  if (feed._tag === 'Failed' && connection._tag === 'Ready') {
    return {
      label: faultText(feed.fault),
      tone: 'warn',
      hint: hintOf(feed.fault, reading),
      canRetry: true,
    };
  }
  if (freshnessOf(connection, feed, hasData) === 'live') return null;
  return {
    label: hasData ? 'Catching up' : 'Connecting',
    tone: 'live',
    hint: hasData ? `Catching up with ${what}.` : `Connecting to read ${what}.`,
    canRetry: false,
  };
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
            {say.stale === undefined ? null : <Chip label={`Stale ${say.stale}`} />}
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
