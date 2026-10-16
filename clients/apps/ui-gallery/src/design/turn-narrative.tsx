/**
 * An agent turn read as narrative (design prototype): thinking and text in time order, tool calls
 * folded into one line between paragraphs that opens in place. Shared by the run-layout and the
 * scale prototypes.
 */
import {Narrative, Said, Thought, ToolGroup, type ToolLine} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {Beat, Entry} from './run-data.js';

/** A turn's beats and the tool calls its tool beats name by id. */
export interface Turn {
  readonly beats: readonly Beat[];
  readonly entries: readonly Entry[];
}

/** The group holding a tool call, so opening that call at level 3 shows it in place. */
function groupOf(turn: Turn, id: string | null): string | null {
  const beat = turn.beats.find(b => b.kind === 'tools' && b.tools.includes(id ?? ''));
  return beat === undefined ? null : beat.id;
}

/** Pressing a tool row opens that call (`open`); `start: 'end'` opens at the last beat (a live turn). */
export function TurnNarrative(props: {
  readonly turn: Turn;
  readonly selected: string | null;
  readonly initiallyOpen: string | null;
  readonly open: (id: string) => void;
  readonly start?: 'top' | 'end';
}): JSX.Element {
  const {turn} = props;
  const [opened, setOpened] = useState<ReadonlySet<string>>(
    () =>
      new Set(
        [props.initiallyOpen, groupOf(turn, props.selected)].filter((g): g is string => g !== null),
      ),
  );
  const byId = new Map(turn.entries.map(e => [e.id, e]));
  const toggle = (group: string, on: boolean): void => {
    const next = new Set(opened);
    if (on) next.add(group);
    else next.delete(group);
    setOpened(next);
  };
  return (
    <Narrative label="Turn transcript" start={props.start ?? 'top'}>
      {turn.beats.map(beat => {
        if (beat.kind === 'thinking') return <Thought key={beat.id} text={beat.text} />;
        if (beat.kind === 'text') return <Said key={beat.id} text={beat.text} />;
        const tools: ToolLine[] = beat.tools.flatMap(id => {
          const e = byId.get(id);
          if (e === undefined) throw new Error(`no turn entry ${id}`);
          return [
            {
              id: e.id,
              name: e.name,
              line: e.line,
              ...(e.meta === undefined ? {} : {outcome: e.meta}),
              state: e.state,
              selected: props.selected === e.id,
              press: () => props.open(e.id),
            },
          ];
        });
        return (
          <ToolGroup
            key={beat.id}
            tools={tools}
            open={opened.has(beat.id)}
            toggle={on => toggle(beat.id, on)}
          />
        );
      })}
    </Narrative>
  );
}
