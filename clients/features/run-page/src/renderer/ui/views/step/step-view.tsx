/**
 * An opened step's body (the frame draws its pane): an agent turn read as narrative (the
 * structured reply first and collapsible, then thinking and text in time order, tool calls folded
 * into one line between paragraphs that opens in place, a failed call always its own red line),
 * or a gate's verdict, numbers and log; and, at level 3, one item of the step in full (a tool
 * call's outcome, input and result).
 *
 * A live turn opens at its end and offers "New below" when scrolled up; a finished one opens at
 * the top. At level 3 the narrative stays as the left pane with the opened call's group open and
 * its row highlighted. Long turns and logs are windowed by the components.
 */
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {
  Chip,
  EmptyState,
  KeyValue,
  LogPane,
  Narrative,
  Row,
  Said,
  StructuredReply,
  Text,
  Thought,
  ToolGroup,
  type ToolLine,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type GateView,
  gateView,
  itemLines,
  type NarrativeBlock,
  toolOf,
  turnMeta,
  turnNarrative,
} from '../../../../state/step-narrative.js';
import type {PaneHeader, ViewBase, WithCore} from '../props.js';

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

const LIVE = new Set(['running', 'waiting', 'pending']);

function lineOf(
  tool: Extract<NarrativeBlock, {kind: 'tools'}>['tools'][number],
  props: StepViewProps,
): ToolLine {
  return {
    id: tool.id,
    name: tool.name,
    line: tool.summary ?? '',
    ...(tool.meta === undefined ? {} : {outcome: tool.meta}),
    state: tool.state,
    selected: tool.id === props.item,
    press: () => props.zoom({type: 'open-item', item: tool.id}),
  };
}

function BlockView(args: {
  readonly block: NarrativeBlock;
  readonly props: StepViewProps;
  readonly replyOpen: boolean;
  readonly setReplyOpen: (open: boolean) => void;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const {block} = args;
  switch (block.kind) {
    case 'reply':
      return (
        <StructuredReply
          author={block.reply.author}
          title={block.reply.title}
          fields={block.reply.fields}
          raw={block.reply.raw}
          open={args.replyOpen}
          toggle={args.setReplyOpen}
        />
      );
    case 'thought':
      return <Thought text={block.text} />;
    case 'said':
      return <Said text={block.text} />;
    case 'tools':
      return (
        <ToolGroup
          tools={block.tools.map(tool => lineOf(tool, args.props))}
          open={args.open}
          toggle={args.toggle}
        />
      );
  }
}

function TurnBody(props: StepViewProps): JSX.Element {
  const {step, item} = props;
  const [replyOpen, setReplyOpen] = useState(!props.compact);
  // A group the reader opened or closed; any other is open only when it holds the opened call.
  const [flipped, setFlipped] = useState<ReadonlyMap<string, boolean>>(new Map());
  const narrative = turnNarrative(props.model.trace, props.core, step.span);
  if (narrative === null) return <EmptyState title="Reading the turn…" />;
  if (narrative.blocks.length === 0) {
    return (
      <EmptyState
        title={LIVE.has(step.status) ? 'Waiting for the agent' : 'Nothing recorded for this step'}
      />
    );
  }
  const live = LIVE.has(step.status);
  const holding = item === null ? null : toolOf(narrative, item)?.block;
  return (
    <>
      <Narrative key={step.span} label="Agent turn" size="fill" start={live ? 'end' : 'top'}>
        {narrative.blocks.map(block => (
          <BlockView
            key={block.id}
            block={block}
            props={props}
            replyOpen={replyOpen}
            setReplyOpen={setReplyOpen}
            open={flipped.get(block.id) ?? block.id === holding}
            toggle={open => setFlipped(new Map(flipped).set(block.id, open))}
          />
        ))}
      </Narrative>
    </>
  );
}

function GateBody(props: StepViewProps & {readonly gate: GateView}): JSX.Element {
  const {gate} = props;
  return (
    <>
      <Row gap="space-3">
        <Chip label={gate.verdict.label} tone={gate.verdict.tone} dot />
      </Row>
      {gate.numbers.length === 0 ? null : (
        <KeyValue items={gate.numbers.map(([key, value]) => ({key, value, mono: true}))} />
      )}
      {gate.lines.length === 0 ? (
        <EmptyState title="No log recorded for this gate" />
      ) : (
        <LogPane label="Gate log" lines={gate.lines} size="fill" />
      )}
    </>
  );
}

/** The value the idea's parent measured, for the ratio to it; null for the input or an agent. */
function parentValue(props: StepViewProps): number | null {
  const parent = props.idea?.parent ?? null;
  if (parent === null) return null;
  const found = props.model.candidates.candidates.find(each => each.id === parent);
  return found?.measurement?.metricValue ?? null;
}

/** The agent turn's facts for its pane header: how long, how many tools, how many failed. */
export function stepHeader(props: StepViewProps): PaneHeader {
  if (props.step.kind === 'gate') return {};
  const narrative = turnNarrative(props.model.trace, props.core, props.step.span);
  if (narrative === null) return {};
  const end = props.step.endedAt ?? props.clock.now;
  const seconds = end === null ? null : end - props.step.startedAt;
  return {
    meta: (
      <Text variant="small" tone="muted">
        {turnMeta(seconds, narrative)}
      </Text>
    ),
  };
}

export function StepView(props: StepViewProps): JSX.Element {
  if (props.step.kind !== 'gate') return <TurnBody {...props} />;
  const gate = gateView(props.model.trace, props.core, props.step.span, parentValue(props));
  if (gate === null) return <EmptyState title="This gate is not in the run" />;
  return <GateBody {...props} gate={gate} />;
}

export interface ItemViewProps extends ViewBase, WithCore {
  readonly step: CandidateStep;
  /** The item's row id (`StepViewProps.item`). */
  readonly item: string;
}

const OUTCOME = {
  ok: {label: 'Done', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  pending: {label: 'Running', tone: 'live'},
} as const;

/** Level 3: the item in full, filling the pane: its outcome, then its input, then its result. */
export function ItemView(props: ItemViewProps): JSX.Element {
  const narrative = turnNarrative(props.model.trace, props.core, props.step.span);
  const found = narrative === null ? null : toolOf(narrative, props.item);
  if (found === null) return <EmptyState title="This item is not in the step" />;
  const {tool} = found;
  const outcome = OUTCOME[tool.state];
  return (
    <>
      <Row gap="space-3">
        <Chip label={outcome.label} tone={outcome.tone} dot />
        {tool.meta === undefined ? null : (
          <Text variant="small" tone="muted">
            {tool.meta}
          </Text>
        )}
      </Row>
      <LogPane label={`${tool.name} call`} lines={itemLines(tool)} size="fill" />
    </>
  );
}

/** The item's pane header: the call's name and its subject, in place of its row id. */
export function itemHeader(props: ItemViewProps): PaneHeader {
  const narrative = turnNarrative(props.model.trace, props.core, props.step.span);
  const found = narrative === null ? null : toolOf(narrative, props.item);
  if (found === null) return {};
  const {tool} = found;
  return {kicker: `tool call · ${tool.name}`, title: tool.summary ?? tool.name};
}
