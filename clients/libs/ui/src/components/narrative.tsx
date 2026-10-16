import type {JSX, ReactNode} from 'react';
import {Icon} from './icon.js';
import type {ToolState} from './message.js';

/**
 * A turn read as a narrative: `Thought`, `Said` and `ToolGroup` blocks in time order, the words
 * first and the tool calls folded between them. It scrolls inside its pane.
 */
export function Narrative(props: {
  readonly label: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <section className="vs-narrative" aria-label={props.label}>
      {props.children}
    </section>
  );
}

/** The thinking: muted and italic, so it stands apart from what was said. */
export function Thought(props: {readonly text: string}): JSX.Element {
  return <p className="vs-thought">{props.text}</p>;
}

/** What was said, in body text. */
export function Said(props: {readonly text: string}): JSX.Element {
  return <p className="vs-said">{props.text}</p>;
}

/** One tool call inside a `ToolGroup`: its name, its subject and its outcome in a few words. */
export interface ToolLine {
  readonly id: string;
  readonly name: string;
  readonly line: string;
  readonly outcome?: string;
  readonly state: ToolState;
  readonly selected?: boolean;
  readonly press: () => void;
}

function toolWords(tool: ToolLine): string {
  return `${tool.name} ${tool.line}${tool.outcome === undefined ? '' : ` (${tool.outcome})`}`;
}

function ToolRow(props: {readonly tool: ToolLine}): JSX.Element {
  const {tool} = props;
  return (
    <button
      type="button"
      className="vs-toolgroup-row"
      data-state={tool.state}
      aria-current={tool.selected === true ? 'true' : undefined}
      onClick={tool.press}
    >
      <span className="vs-toolgroup-name">{tool.name}</span>
      <span className="vs-toolgroup-line">{tool.line}</span>
      {tool.outcome === undefined ? null : (
        <span className="vs-toolgroup-outcome">{tool.outcome}</span>
      )}
    </button>
  );
}

/**
 * The tool calls made between two paragraphs, as one dim line ("2 tools · read …") that opens in
 * place to one row per call. A failed call stays visible as its own red row while the group is
 * closed. Pressing a row opens that call.
 */
export function ToolGroup(props: {
  readonly tools: readonly ToolLine[];
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const failed = props.tools.filter(tool => tool.state === 'failed');
  const count = `${props.tools.length} ${props.tools.length === 1 ? 'tool' : 'tools'}`;
  return (
    <div className="vs-toolgroup" data-open={props.open || undefined}>
      <button
        type="button"
        className="vs-toolgroup-head"
        aria-expanded={props.open}
        onClick={() => props.toggle(!props.open)}
      >
        <Icon name={props.open ? 'chevron-down' : 'chevron-right'} />
        <span className="vs-toolgroup-count">{count}</span>
        <span className="vs-toolgroup-line">{props.tools.map(toolWords).join(', ')}</span>
      </button>
      {(props.open ? props.tools : failed).map(tool => (
        <ToolRow key={tool.id} tool={tool} />
      ))}
    </div>
  );
}
