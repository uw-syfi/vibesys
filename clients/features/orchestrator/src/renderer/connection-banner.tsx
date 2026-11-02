import {Button, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {Fault, StreamStatus} from '../state/index.js';
import {faultWord} from './words.js';

/** One thing about a connection a person should know, with the way to try again. */
export interface ConnectionNote {
  readonly key: string;
  /** What the note is about: a host's runs, or one agent's steps on it. */
  readonly what: string;
  readonly status: StreamStatus;
  readonly fault: Fault | null;
  /** The screen already shows something read earlier, so the note says it may be out of date. */
  readonly hasData: boolean;
  readonly retry: () => void;
}

/** The notes worth showing: nothing for a connection that is working. */
const notesOf = (notes: readonly ConnectionNote[]): ConnectionNote[] =>
  notes.filter(note => note.status !== 'live');

function wording(note: ConnectionNote): string {
  if (note.status === 'connecting') {
    return note.hasData ? `Catching up with ${note.what}.` : `Connecting to read ${note.what}.`;
  }
  const why = note.fault === null ? 'The connection was lost.' : faultWord(note.fault.kind);
  const next = note.status === 'gave-up' ? 'Gave up trying.' : 'Trying again.';
  return note.hasData
    ? `${why} What is shown may be out of date. ${next}`
    : `${why} Nothing has been read from ${note.what} yet. ${next}`;
}

/**
 * The state of every connection that is not working, on every screen: connecting, catching up, or
 * lost with the reason and a Retry. A working connection shows nothing.
 */
export function ConnectionBanner(props: {
  readonly notes: readonly ConnectionNote[];
}): JSX.Element | null {
  const shown = notesOf(props.notes);
  if (shown.length === 0) return null;
  return (
    <div role="status" aria-live="polite">
      {shown.map(note => (
        <Row key={note.key} gap="space-3" wrap>
          <Text tone={note.status === 'connecting' ? 'muted' : 'warn'}>{wording(note)}</Text>
          {note.fault === null || note.status === 'connecting' ? null : (
            <Text tone="muted" variant="small">{`Details: ${note.fault.detail}`}</Text>
          )}
          {note.status !== 'connecting' ? (
            <Button text="Retry" variant="secondary" size="sm" press={note.retry} />
          ) : null}
        </Row>
      ))}
    </div>
  );
}
