import {Button, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type Fault,
  type FeedStatus,
  freshnessOf,
  type HostConnectionState,
} from '../../state/index.js';
import {faultWord, formatDuration} from './words.js';

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

interface Say {
  readonly context?: string;
  readonly text: string;
  readonly tone: 'muted' | 'warn';
  readonly fault: Fault | null;
  readonly canRetry: boolean;
  /** How long what is shown has been out of date, when that is worth saying. */
  readonly staleForMs?: number | null;
}

/** What to tell the person, or null when the connection is working and nothing needs saying. */
function sayAbout(note: ConnectionNote): Say | null {
  const {connection, feed, hasData, what} = note;
  const staleTail = hasData
    ? 'What is shown may be out of date.'
    : `Nothing has been read from ${what} yet.`;
  if (connection._tag === 'Incompatible') {
    const fix =
      connection.side === 'client-too-old' ? 'Update this app.' : 'Update VibeSys on that host.';
    return {
      text: `This app and the VibeSys on the host cannot work together. ${fix}`,
      tone: 'warn',
      fault: null,
      canRetry: true,
    };
  }
  if (connection._tag === 'Unusable') {
    return {
      text: faultWord(connection.fault.kind),
      context: `Automatic retries stopped. ${hasData ? staleTail : 'No runs have been read.'}`,
      tone: 'warn',
      fault: connection.fault,
      canRetry: true,
    };
  }
  if (connection._tag === 'Waiting') {
    return {
      text: faultWord(connection.fault.kind),
      context: `${staleTail} Trying again.`,
      tone: 'warn',
      fault: connection.fault,
      canRetry: true,
      staleForMs: note.staleForMs,
    };
  }
  const freshness = freshnessOf(connection, feed, hasData);
  if (feed._tag === 'Failed' && connection._tag === 'Ready') {
    return {
      text: faultWord(feed.fault.kind),
      context: staleTail,
      tone: 'warn',
      fault: feed.fault,
      canRetry: true,
    };
  }
  if (freshness === 'live') return null;
  return {
    text: hasData ? `Catching up with ${what}.` : `Connecting to read ${what}.`,
    tone: 'muted',
    fault: null,
    canRetry: false,
    staleForMs: note.staleForMs,
  };
}

/**
 * The state of every connection that is not working, on every screen: connecting, catching up, or
 * lost with the reason and a Retry. A working connection shows nothing.
 */
export function ConnectionBanner(props: {
  readonly notes: readonly ConnectionNote[];
}): JSX.Element | null {
  const shown = props.notes.flatMap(note => {
    const say = sayAbout(note);
    return say === null ? [] : [{note, say}];
  });
  if (shown.length === 0) return null;
  return (
    <div role="status" aria-live="polite" aria-label="Connection">
      {shown.map(({note, say}) => (
        <Stack key={note.key} gap="space-2" align="start">
          <Text tone={say.tone}>{say.text}</Text>
          {say.context === undefined ? null : <Text variant="small">{say.context}</Text>}
          {say.staleForMs === undefined || say.staleForMs === null ? null : (
            <Text
              tone="muted"
              variant="small"
            >{`Stale \u00b7 ${formatDuration(say.staleForMs)}`}</Text>
          )}
          <Row gap="space-3" wrap>
            {say.fault === null ? null : (
              <Text tone="muted" variant="small">{`Details: ${say.fault.detail}`}</Text>
            )}
            {say.canRetry ? (
              <Button
                text="Retry connection"
                label={`Retry connection to read ${note.what}`}
                variant="secondary"
                size="sm"
                press={note.tryAgain}
              />
            ) : null}
          </Row>
        </Stack>
      ))}
    </div>
  );
}
