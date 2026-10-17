/**
 * An opened step's body (the frame draws its pane): an agent turn read as narrative (the
 * structured reply first, then thinking and text in time order, tool groups folded and opened in
 * place, failed tools as red lines), or a gate's measurement and log; and, at level 3, one item of
 * the step in full (a tool call's input and output).
 *
 * Stub: the turn's tool calls as a list that opens each (from the transcript rows
 * `turnTranscript` already reads), and the opened tool call in full; the narrative and the gate
 * are still to build (#2997).
 */
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {EmptyState, PickList, PickRow, ToolCall} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {type TurnRow, turnTranscript} from '../../../../state/index.js';
import type {ViewBase, WithCore} from '../props.js';

export interface StepViewProps extends ViewBase, WithCore {
  /** The idea the step belongs to; null under an opened agent. */
  readonly idea: Candidate | null;
  /** The agent whose turn it is, when opened from the Agents face. */
  readonly agent: AgentNode | null;
  readonly step: CandidateStep;
  /** The opened item (a transcript entry's row id), highlighted; null at level 2. */
  readonly item: string | null;
  /** Beside the opened item (level 3): the narrow left pane. */
  readonly compact: boolean;
}

type ToolRow = Extract<TurnRow, {kind: 'tool'}>;

/** The step's tool calls, in order. */
function toolRows(props: ViewBase & WithCore & {readonly step: CandidateStep}): ToolRow[] {
  const transcript = turnTranscript(props.model.trace, props.core, {
    filter: 'all',
    open: new Set(),
    selected: props.step.span,
  });
  return (transcript?.rows ?? []).filter((row): row is ToolRow => row.kind === 'tool');
}

export function StepView(props: StepViewProps): JSX.Element {
  const tools = toolRows(props);
  if (tools.length === 0) return <EmptyState title="Nothing recorded for this step yet" />;
  return (
    <PickList
      label="Tool calls"
      size="fill"
      {...(props.item === null ? {} : {selected: props.item})}
    >
      {tools.map(tool => (
        <PickRow
          key={tool.id}
          id={tool.id}
          lead={tool.name}
          title={tool.summary ?? tool.name}
          mono
          state={
            tool.state === 'failed'
              ? {tone: 'err', label: 'failed'}
              : tool.state === 'pending'
                ? {tone: 'live', label: 'running'}
                : {tone: 'ok', label: 'done'}
          }
          select={() => props.zoom({type: 'open-item', item: tool.id})}
        />
      ))}
    </PickList>
  );
}

export interface ItemViewProps extends ViewBase, WithCore {
  readonly step: CandidateStep;
  /** The item's row id (`StepViewProps.item`). */
  readonly item: string;
}

/** Level 3: the item in full. */
export function ItemView(props: ItemViewProps): JSX.Element {
  const [open, setOpen] = useState(true);
  const tool = toolRows(props).find(row => row.id === props.item);
  if (tool === undefined) return <EmptyState title="This item is not in the step" />;
  return (
    <ToolCall
      name={tool.name}
      state={tool.state}
      {...(tool.summary === undefined ? {} : {summary: tool.summary})}
      {...(tool.meta === undefined ? {} : {meta: tool.meta})}
      input={tool.input}
      output={tool.output}
      open={open}
      toggle={setOpen}
    />
  );
}
