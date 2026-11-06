import {
  type Agent,
  Delivery,
  ItemStatus,
  type Message,
  oneofIs,
  type Receipt,
  type ToolCall,
  TurnTrigger,
} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  Field,
  KeyValue,
  Row,
  Stack,
  Text,
  TextInput,
  Thread,
  type ThreadItem,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {
  agentThread,
  messagesOf,
  ms,
  type OrchestratorState,
  type ThreadItem as ShownItem,
  type ThreadTurn,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {
  agentWord,
  deliveryWord,
  describeCall,
  formatClock,
  formatDuration,
  harnessWord,
  refusalWord,
  rejectionWord,
  roleWord,
  turnWord,
  type Word,
} from './words.js';

const TRIGGER: Readonly<Record<TurnTrigger, string>> = {
  [TurnTrigger.UNSPECIFIED]: 'Started',
  [TurnTrigger.BRIEF]: 'Started by its assignment',
  [TurnTrigger.OPERATOR]: 'Started by your message',
  [TurnTrigger.HANDOFF]: 'Started by a handoff',
  [TurnTrigger.JOB_FINISHED]: 'Woken by a finished job',
  [TurnTrigger.SCHEDULE]: 'Started on a schedule',
  [TurnTrigger.MESSAGE]: 'Started by a message from another agent',
  [TurnTrigger.SUBAGENT_SETTLED]: 'Woken because its helpers finished',
  [TurnTrigger.REMINDER]: 'Reminded of what it still owes',
};

function toolItem(shown: ShownItem, call: ToolCall): ThreadItem {
  const {item} = shown;
  const running = item.status === ItemStatus.RUNNING;
  const {name, asked} = describeCall(call);
  return {
    kind: 'tool',
    id: item.id,
    name,
    state: running ? 'pending' : call.isError ? 'failed' : 'ok',
    summary: (asked ?? '').split('\n')[0]?.slice(0, 80) ?? '',
    input: asked,
    output: shown.output === '' ? null : shown.output,
    ...(running ? {meta: 'In progress'} : {}),
  };
}

/** Who a written item is from. The message's own label already says what kind it is. */
function speaker(kind: 'prompt' | 'reasoning' | 'assistant', name: string) {
  return kind === 'prompt'
    ? ({author: `Given to ${name}`, variant: 'prompt'} as const)
    : kind === 'reasoning'
      ? ({author: name, variant: 'reasoning'} as const)
      : ({author: name, variant: 'reply'} as const);
}

function threadItem(shown: ShownItem, name: string): ThreadItem {
  const {item} = shown;
  const body = item.body;
  if (oneofIs(body, 'tool', {assistant: true, reasoning: true, prompt: true, tool: true}))
    return toolItem(shown, body.value);
  return {
    kind: 'message',
    id: item.id,
    ...speaker(
      oneofIs(body, 'prompt', {assistant: true, reasoning: true, prompt: true}) ||
        oneofIs(body, 'reasoning', {assistant: true, reasoning: true})
        ? body.case
        : 'assistant',
      name,
    ),
    text: shown.text,
    ...(item.status === ItemStatus.RUNNING ? {notice: 'In progress'} : {}),
    startOpen: true,
  };
}

function turnItems(turn: ThreadTurn, name: string): ThreadItem[] {
  const head: ThreadItem[] = [];
  if (turn.missingBefore) {
    head.push({
      kind: 'heading',
      id: `gap-${turn.turnId}`,
      title: 'Earlier steps are not shown',
      detail: 'Only the most recent steps of this agent are loaded.',
    });
  }
  if (turn.turn !== null) {
    const ran =
      (ms(turn.turn.endedAt) ?? ms(turn.turn.startedAt) ?? 0) - (ms(turn.turn.startedAt) ?? 0);
    head.push({
      kind: 'heading',
      id: `turn-${turn.turnId}`,
      title: `Turn ${turn.turn.ordinal}: ${TRIGGER[turn.turn.trigger]}`,
      detail: `${turnWord(turn.turn.status)}, ${formatDuration(ran)}`,
    });
  }
  return [...head, ...turn.items.map(shown => threadItem(shown, name))];
}

/** What this agent's tool lets a message do, in one sentence. */
function capabilityLine(agent: Agent): string {
  const caps = agent.capabilities;
  const takes =
    caps?.steers === true
      ? 'A message reaches it in the middle of what it is doing.'
      : 'A message waits until it finishes its current step.';
  return `${harnessWord(agent.harness)}: ${takes}`;
}

/** What a stored message's delivery says now, with the reason when it was rejected. */
function storedLine(message: Message): {label: string; tone: Word['tone']} {
  const word = deliveryWord(message.delivery);
  const why = message.delivery === Delivery.REJECTED ? ` ${rejectionWord(message.rejection)}` : '';
  return {label: `${word.label}.${why}`, tone: word.tone};
}

/** The line for an answer whose message the host has not reported yet: the receipt's own words. */
function receiptLine(receipt: Receipt): {label: string; tone: Word['tone']} {
  if (receipt.refusal !== 0) return {label: refusalWord(receipt.refusal), tone: 'err'};
  const word = deliveryWord(receipt.delivery);
  return {label: `${word.label}.`, tone: word.tone};
}

function SentMessage(props: {
  readonly text: string;
  readonly atMs: number | null;
  readonly line: {readonly label: string; readonly tone: Word['tone']};
}): JSX.Element {
  return (
    <Row gap="space-3" wrap>
      <Text tone="muted">{`You wrote: ${props.text}`}</Text>
      <Chip label={props.line.label} tone={props.line.tone} dot />
      {props.atMs === null ? null : (
        <Text variant="small">{`Sent at ${formatClock(props.atMs)}`}</Text>
      )}
    </Row>
  );
}

/** The messages sent so far, each with where it stands now, and the last answer until the host reports it. */
function SentMessages(props: {
  readonly pendingAtMs: number | null;
  readonly sent: readonly Message[];
  readonly progress: CommandProgress | undefined;
}): JSX.Element {
  const {progress} = props;
  const pending =
    progress?._tag === 'Accepted' &&
    !props.sent.some(message => message.id === progress.receipt.messageId)
      ? progress.receipt
      : null;
  return (
    <>
      {props.sent.map(message => (
        <SentMessage
          key={message.id}
          text={
            oneofIs(message.body, 'text', {
              text: true,
              handoff: true,
              report: true,
              jobFinished: true,
              subagentSettled: true,
              brief: true,
              leadBrief: true,
              handoffReminder: true,
              leadIdleReminder: true,
              runLimitReached: true,
            })
              ? message.body.value
              : ''
          }
          atMs={ms(message.at)}
          line={storedLine(message)}
        />
      ))}
      {pending === null ? null : (
        <SentMessage
          atMs={props.pendingAtMs}
          text="your last message"
          line={receiptLine(pending)}
        />
      )}
    </>
  );
}

function MessageBox(props: {
  readonly nowMs: number;
  readonly agent: Agent;
  /** What the last press of Send came to, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  /** The messages the person sent to this agent, as the host last reported them. */
  readonly sent: readonly Message[];
  readonly send: (text: string) => void;
  readonly retry: () => void;
}): JSX.Element {
  const [draft, setDraft] = useState('');
  const [sentAtMs, setSentAtMs] = useState<number | null>(null);
  const [sentText, setSentText] = useState<string | null>(null);
  const {progress} = props;
  const sending = progress?._tag === 'Sending';
  // The text stays until the host has taken the message; only then is the box empty again.
  const taken = progress?._tag === 'Accepted' && sentText === draft;
  const shown = taken ? '' : draft;
  return (
    <Card section={`Message ${roleWord(props.agent.role)}`}>
      <Stack gap="space-3">
        <Field label="Your message" help={capabilityLine(props.agent)}>
          <TextInput
            value={shown}
            change={setDraft}
            placeholder="Write a message to this agent"
            rows={2}
          />
        </Field>
        <Row>
          <Button
            text={sending ? 'Sending' : 'Send'}
            label="Send message"
            variant="primary"
            press={() => {
              setDraft(shown);
              setSentText(shown);
              setSentAtMs(props.nowMs);
              props.send(shown);
            }}
            {...(shown.trim() === '' || sending
              ? {disabledReason: sending ? 'Sending.' : 'Write a message first.', quietReason: true}
              : {})}
          />
        </Row>
        <CommandLine progress={progress} done="Send" retry={props.retry} />
        <SentMessages pendingAtMs={sentAtMs} sent={props.sent} progress={progress} />
      </Stack>
    </Card>
  );
}

/** One agent: who it is, every turn it has taken (past and live in one list), and a message box. */
export function AgentPanel(props: {
  readonly state: OrchestratorState;
  readonly nowMs: number;
  readonly agent: Agent;
  readonly name: string;
  /** What the last press of Send to this agent came to, or undefined before any. */
  readonly sendProgress: CommandProgress | undefined;
  readonly send: (text: string) => void;
  readonly retrySend: () => void;
  readonly close: () => void;
}): JSX.Element {
  const {state, agent} = props;
  const turns = agentThread(state, agent.id);
  const status = agentWord(agent.status);
  return (
    <Stack gap="space-4">
      <Row gap="space-3" justify="between">
        <Row gap="space-3">
          <Text variant="heading">{props.name}</Text>
          <Chip label={status.label} tone={status.tone} dot />
          <Chip label={harnessWord(agent.harness)} />
          <Chip label={agent.model} mono />
        </Row>
        <Button text="Close agent view" variant="quiet" size="sm" press={props.close} />
      </Row>
      <KeyValue
        items={[
          {key: 'Tool', value: harnessWord(agent.harness)},
          {key: 'Effort', value: agent.reasoningEffort === '' ? 'Default' : agent.reasoningEffort},
        ]}
      />
      <MessageBox
        nowMs={props.nowMs}
        agent={agent}
        progress={props.sendProgress}
        sent={messagesOf(state, agent.id).filter(
          message =>
            message.fromAgentId === '' &&
            oneofIs(message.body, 'text', {
              text: true,
              handoff: true,
              report: true,
              jobFinished: true,
              subagentSettled: true,
              brief: true,
              leadBrief: true,
              handoffReminder: true,
              leadIdleReminder: true,
              runLimitReached: true,
            }),
        )}
        send={props.send}
        retry={props.retrySend}
      />
      {turns.length === 0 ? (
        <EmptyState title="Nothing yet" body="This agent has not taken a turn yet." />
      ) : (
        <Thread
          label={`${props.name}, turn by turn`}
          items={turns.flatMap(turn => turnItems(turn, props.name))}
        />
      )}
    </Stack>
  );
}
