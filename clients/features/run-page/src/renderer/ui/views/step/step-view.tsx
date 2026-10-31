/**
 * An opened step's body (the frame draws its pane): an agent turn read as narrative (thinking and
 * text in time order, tool calls folded into one line between paragraphs that opens in place, a
 * failed call always its own red line, and the structured reply last as a card, with a one-line
 * summary of it above the narrative that scrolls to it when tool calls put the card out of sight),
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
  Button,
  Chip,
  EmptyState,
  KeyValue,
  LogPane,
  markdownToPlain,
  Narrative,
  Row,
  Said,
  Text,
  Thought,
  ToolGroup,
  type ToolLine,
  TurnResult,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  stepKicker,
  tokensLine,
  tokensTitle,
  turnTokens,
  whoWords,
} from '../../../../state/index.js';
import {workedSeconds} from '../../../../state/quota-pauses.js';
import {elapsedWords} from '../../../../state/span-reading.js';
import {
  type BlockStart,
  blockStarts,
  type GateView,
  gateView,
  itemLines,
  type NarrativeBlock,
  type TurnNarrative,
  toolOf,
  turnMeta,
  turnNarrative,
} from '../../../../state/step-narrative.js';
import {type TurnResultView, turnResult} from '../../../../state/turn-result.js';
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
    ...(tool.elapsed === undefined
      ? {}
      : {
          time: `+${elapsedWords(tool.elapsed)}`,
          timeTitle: `${elapsedWords(tool.elapsed)} into the turn`,
        }),
    state: tool.state,
    selected: tool.id === props.item,
    press: () => props.zoom({type: 'open-item', item: tool.id}),
  };
}

function ResultCard(args: {
  readonly result: TurnResultView;
  readonly props: StepViewProps;
}): JSX.Element {
  const {result, props} = args;
  const base = result.builtOn;
  const open = base?.idea ?? null;
  return (
    <TurnResult
      label={result.label}
      author={result.author}
      {...(result.headline === null ? {} : {headline: result.headline})}
      chips={result.chips}
      {...(base === null
        ? {}
        : {
            context: {
              prefix: 'builds on',
              label: base.label,
              ...(open === null ? {} : {press: () => props.zoom({type: 'open-idea', idea: open})}),
            },
          })}
      sections={result.sections}
      more={result.more}
      raw={result.raw}
    />
  );
}

function BlockView(args: {
  readonly block: NarrativeBlock;
  readonly props: StepViewProps;
  readonly live: boolean;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const {block} = args;
  switch (block.kind) {
    case 'reply':
      return (
        <ResultCard
          result={turnResult(block.reply, {
            candidates: args.props.model.candidates.candidates,
            idea: args.props.idea,
          })}
          props={args.props}
        />
      );
    case 'thought':
      return <Thought text={block.text} live={args.live} open={args.open} toggle={args.toggle} />;
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

/**
 * The one line that leads a turn whose reply is out of sight: what it ended with. A turn that made
 * tool calls puts its reply card below them, so the line tells the outcome and scrolls to it. A
 * turn of thinking and a reply (a judge's) shows the card at once, and a line above it would only
 * repeat the card: none for it, and none while the turn has no reply.
 */
function resultSummary(narrative: TurnNarrative, props: StepViewProps): string | null {
  const last = narrative.blocks.at(-1);
  if (last?.kind !== 'reply') return null;
  if (!narrative.blocks.some(block => block.kind === 'tools')) return null;
  // The line is one line of plain words, though the reply's prose it quotes is Markdown.
  return markdownToPlain(
    turnResult(last.reply, {
      candidates: props.model.candidates.candidates,
      idea: props.idea,
    }).summary,
  );
}

function TurnBody(props: StepViewProps): JSX.Element {
  const {step, item} = props;
  // Counts the presses of the summary line, which scrolls the narrative to the reply card.
  const [toResult, setToResult] = useState(0);
  // A thought or group the reader opened or closed, or a thought left open while it was written;
  // any other starts as `blockStarts` says, and a group holding the opened call starts open.
  const [flipped, setFlipped] = useState<ReadonlyMap<string, boolean>>(new Map());
  const narrative = turnNarrative(props.model.trace, props.core, step.span);
  if (narrative === null) return <EmptyState title="Reading the turn…" />;
  if (narrative.blocks.length === 0) {
    return (
      <EmptyState
        title={
          props.emptyLabelsOnly
            ? 'No transcript'
            : LIVE.has(step.status)
              ? 'Waiting for the agent'
              : 'Nothing recorded for this step'
        }
      />
    );
  }
  const live = LIVE.has(step.status);
  const starts = blockStarts(narrative, live);
  // A thought open because it is being written stays open when the turn moves on, so a reader who
  // is partway through it is not shown it collapse. Only a press closes it from then on.
  const unlatched = [...starts].filter(([id, start]) => start.live && !flipped.has(id));
  if (unlatched.length > 0) {
    setFlipped(new Map([...flipped, ...unlatched.map(([id]) => [id, true] as const)]));
  }
  const startOf = (id: string): BlockStart => starts.get(id) ?? {live: false, open: false};
  const holding = item === null ? null : toolOf(narrative, item)?.block;
  const summary = resultSummary(narrative, props);
  return (
    <>
      {summary === null ? null : (
        <Row gap="space-2">
          <Button
            size="sm"
            variant="quiet"
            text={summary}
            press={() => setToResult(toResult + 1)}
          />
        </Row>
      )}
      <Narrative
        key={step.span}
        label="Agent turn"
        size="fill"
        start={live ? 'end' : 'top'}
        toEnd={toResult}
      >
        {narrative.blocks.map(block => (
          <BlockView
            key={block.id}
            block={block}
            props={props}
            live={startOf(block.id).live}
            open={flipped.get(block.id) ?? (startOf(block.id).open || block.id === holding)}
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
  const kicker = stepKicker(props.step);
  const narrative = turnNarrative(props.model.trace, props.core, props.step.span);
  if (narrative === null) return {kicker};
  const end = props.step.endedAt ?? props.clock.now;
  const seconds =
    end === null ? null : workedSeconds(props.model.pauses, props.step.startedAt, end);
  const spent = turnTokens(props.tokens, props.step.span);
  const tokens = spent === null ? null : tokensLine(spent);
  const ran = whoWords(props.who.get(props.step.span));
  return {
    kicker,
    meta: (
      <Row gap="space-3" align="baseline">
        {ran === null ? null : (
          <Text variant="code" tone="muted">
            {ran}
          </Text>
        )}
        <Text
          variant="small"
          tone="muted"
          {...(spent === null || tokens === null ? {} : {title: tokensTitle(spent)})}
        >
          {turnMeta(seconds, narrative, tokens)}
        </Text>
      </Row>
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
      <LogPane label={`${tool.name} call`} lines={itemLines(tool)} size="fill" start="top" />
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
