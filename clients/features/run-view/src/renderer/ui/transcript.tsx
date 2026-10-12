/** A fixed-row virtual transcript. The run fold owns event projection; this component only windows
 * already-projected entries so a replay bootstrap cannot create one DOM node per event. */

import type {TranscriptEntry} from '@vibesys/run-state/transcript';
import {type JSX, useCallback, useState} from 'react';

const ROW_HEIGHT_PX = 144;
const VIEWPORT_HEIGHT_PX = 576;
const OVERSCAN_ROWS = 8;

export interface TranscriptProps {
  readonly entries: readonly TranscriptEntry[];
  readonly emptyCopy: string;
}

export function Transcript({entries, emptyCopy}: TranscriptProps): JSX.Element {
  const visible = Math.ceil(VIEWPORT_HEIGHT_PX / ROW_HEIGHT_PX) + OVERSCAN_ROWS * 2;
  const [viewport, setViewport] = useState({scrollTop: 0, pinned: true});
  const maxScrollTop = Math.max(0, entries.length * ROW_HEIGHT_PX - VIEWPORT_HEIGHT_PX);
  const scrollTop = viewport.pinned ? maxScrollTop : Math.min(viewport.scrollTop, maxScrollTop);
  const first = Math.min(
    Math.max(0, Math.floor(scrollTop / ROW_HEIGHT_PX) - OVERSCAN_ROWS),
    Math.max(0, entries.length - visible),
  );
  const last = Math.min(entries.length, first + visible);
  const placeViewport = useCallback(
    (node: HTMLElement | null): void => {
      if (node !== null && node.scrollTop !== scrollTop) node.scrollTop = scrollTop;
    },
    [scrollTop],
  );
  if (entries.length === 0) return <p className="empty">{emptyCopy}</p>;
  return (
    <section
      ref={placeViewport}
      className="virtual-transcript"
      onScroll={event => {
        const nextScrollTop = event.currentTarget.scrollTop;
        setViewport({
          scrollTop: nextScrollTop,
          pinned: maxScrollTop - nextScrollTop <= 1,
        });
      }}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: This overflow viewport must receive focus so keyboard users can scroll its virtualized list.
      tabIndex={0}
      aria-label={`Transcript, ${entries.length} folded events`}
    >
      <ol
        className="transcript"
        style={{height: entries.length * ROW_HEIGHT_PX}}
        aria-label="Transcript events"
      >
        {entries.slice(first, last).map((entry, offset) => {
          const index = first + offset;
          return (
            <TranscriptRow key={entry.id} entry={entry} index={index} setSize={entries.length} />
          );
        })}
      </ol>
    </section>
  );
}

function TranscriptRow({
  entry,
  index,
  setSize,
}: {
  readonly entry: TranscriptEntry;
  readonly index: number;
  readonly setSize: number;
}): JSX.Element {
  const title = entry.toolName ?? entry.label ?? entry.kind;
  const toolArguments =
    entry.toolArguments === undefined ? null : JSON.stringify(entry.toolArguments, null, 2);
  const content = transcriptContent(entry);
  return (
    <li
      className={`transcript-row transcript-${entry.kind}${entry.tone === undefined ? '' : ` transcript-${entry.tone}`}`}
      style={{transform: `translateY(${index * ROW_HEIGHT_PX}px)`}}
      aria-posinset={index + 1}
      aria-setsize={setSize}
    >
      <div className="transcript-row-heading">
        <span>{title}</span>
        <span>{entry.roundNumber === undefined ? 'Run' : `Round ${entry.roundNumber}`}</span>
      </div>
      {toolArguments !== null && (
        <section
          className="tool-arguments"
          aria-label={`${title} tool arguments`}
          // biome-ignore lint/a11y/noNoninteractiveTabindex: This named region owns clipped overflow and must receive keyboard scrolling; a textarea would invent editing semantics, while details would hide run evidence.
          tabIndex={0}
        >
          <pre>{toolArguments}</pre>
        </section>
      )}
      <section
        className="transcript-content"
        aria-label={`${title} output`}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: This named region owns clipped overflow and must receive keyboard scrolling; a textarea would invent editing semantics, while details would hide run evidence.
        tabIndex={0}
      >
        <pre>{content}</pre>
      </section>
      {entry.truncated === true && (
        <span className="event-truncated">Event payload truncated at the server's size bound.</span>
      )}
    </li>
  );
}

function transcriptContent(entry: TranscriptEntry): string {
  if (entry.content.length > 0) return entry.content;
  if (entry.toolResult !== undefined) return 'No tool output was returned.';
  if (entry.kind === 'tool' && entry.toolArguments !== undefined) {
    return 'Waiting for tool output…';
  }
  return '';
}
