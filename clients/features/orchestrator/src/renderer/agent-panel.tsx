import {
  type Agent,
  Delivery,
  ItemStatus,
  type Message,
  type Receipt,
  type ToolCall,
  TurnTrigger,
} from '@vibesys/orchestrator-proto';
import {
  agentThread,
  messagesOf,
  ms,
  type OrchestratorState,
  oldestOrdinal,
  type ThreadItem as ShownItem,
  type ThreadTurn,
} from '@vibesys/orchestrator-state';
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
import type {AgentView, MessageBox as Box, Fault} from '../state/index.js';
import {
  agentWord,
  deliveryWord,
  describeArgs,
  faultWord,
  formatDuration,
  harnessWord,
  refusalWord,
  rejectionWord,
  roleWord,
  toolLabel,
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
};

function toolItem(shown: ShownItem, call: ToolCall): ThreadItem {
  const {item} = shown;
  const running = item.status === ItemStatus.RUNNING;
  const asked = describeArgs(call.argsJson);
  return {
    kind: 'tool',
    id: item.id,
    name: toolLabel(call.tool),
    state: running ? 'pending' : call.isError ? 'failed' : 'ok',
    summary: (asked ?? '').split('\n')[0]?.slice(0, 80) ?? '',
    input: asked,
    output: shown.output === '' ? null : shown.output,
    ...(running ? {meta: 'In progress'} : {}),
  };
}

const SPEAKER = {
  prompt: {author: 'Instructions', variant: 'prompt'},
  reasoning: {author: 'Thinking', variant: 'reasoning'},
  assistant: {author: 'Answer', variant: 'reply'},
} as const;

function threadItem(shown: ShownItem): ThreadItem {
  const {item} = shown;
  const body = item.body;
  if (body.case === 'tool') return toolItem(shown, body.value);
  const speaker =
    SPEAKER[body.case === 'prompt' || body.case === 'reasoning' ? body.case : 'assistant'];
  return {
    kind: 'message',
    id: item.id,
    ...speaker,
    text: shown.text,
    ...(item.status === ItemStatus.RUNNING ? {notice: 'In progress'} : {}),
    startOpen: true,
  };
}

function turnItems(turn: ThreadTurn): ThreadItem[] {
  const head: ThreadItem[] = [];
  if (turn.missingBefore) {
    head.push({
      kind: 'heading',
      id: `gap-${turn.turnId}`,
      title: 'Earlier steps are not loaded',
      detail: 'Use "Show earlier steps" above to load them.',
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
  return [...head, ...turn.items.map(threadItem)];
}

/** What this agent's tool lets a message do, in one sentence. */
function capabilityLine(agent: Agent): string {
  const caps = agent.capabilities;
  const takes =
    caps?.steers === true
      ? 'A message reaches it in the middle of what it is doing.'
      : 'A message waits until it finishes its current step.';
  const streams =
    caps?.streamsText === true
      ? 'Its words appear as it writes them.'
      : 'Its words appear when each step ends.';
  const resumes =
    caps?.resumes === true
      ? 'It can pick up again after finishing.'
      : 'It cannot be resumed once finished.';
  return `${harnessWord(agent.harness)}: ${takes} ${streams} ${resumes}`;
}

/** What a stored message's delivery says now, with the reason when it was rejected. */
function storedLine(message: Message): {label: string; tone: Word['tone']} {
  const word = deliveryWord(message.delivery);
  const why =
    message.delivery === Delivery.REJECTED ? ` ${rejectionWord(message.rejection)}` : '';
  return {label: `${word.label}.${why}`, tone: word.tone};
}

/** The line for an answer whose message the host has not reported yet: the receipt's own words. */
function receiptLine(receipt: Receipt): {label: string; tone: Word['tone']} {
  if (receipt.refusal !== 0) return {label: refusalWord(receipt.refusal), tone: 'err'};
  const word = deliveryWord(receipt.delivery);
  return {label: `${word.label}.`, tone: word.tone};
}

function failureLine(fault: Fault): string {
  return `Could not send. ${faultWord(fault.kind)}`;
}

/** What the person is typing, and the id of the send it is retrying (null until it is sent once). */
export interface Draft {
  readonly text: string;
  readonly id: string | null;
}

/**
 * The id a press of Send uses. A message that never got an answer is sent again under the id it
 * already has, so the host cannot deliver it twice; any other press is a new message with a fresh
 * id (an edit clears the draft's id, and a refused message is final under its id).
 */
export function idForSend(draft: Draft, box: Box, newId: () => string): string {
  return box.status === 'failed' && draft.id !== null && box.commandId === draft.id
    ? draft.id
    : newId();
}

function SentMessage(props: {
  readonly text: string;
  readonly line: {readonly label: string; readonly tone: Word['tone']};
}): JSX.Element {
  return (
    <Row gap="space-3" wrap>
      <Text tone="muted">{`You wrote: ${props.text}`}</Text>
      <Chip label={props.line.label} tone={props.line.tone} dot />
    </Row>
  );
}

function MessageBox(props: {
  readonly agent: Agent;
  readonly box: Box;
  /** The messages the person sent to this agent, as the host last reported them. */
  readonly sent: readonly Message[];
  readonly newId: () => string;
  readonly send: (commandId: string, text: string) => void;
  readonly dismiss: () => void;
}): JSX.Element {
  const [draft, setDraft] = useState<Draft>({text: '', id: null});
  const {box} = props;
  const sending = box.status === 'sending';
  // The text stays until the host has taken the message; only then is the box empty again.
  const taken = box.status === 'answered' && box.receipt.refusal === 0 && box.commandId === draft.id;
  const shown = taken ? '' : draft.text;
  const retrying = box.status === 'failed' && box.commandId === draft.id;
  const reported = box.status === 'answered' && props.sent.some(m => m.id === box.receipt.messageId);
  return (
    <Card section={`Message ${roleWord(props.agent.role)}`}>
      <Stack gap="space-3">
        <Field label="Your message" help={capabilityLine(props.agent)}>
          <TextInput
            value={shown}
            change={text => setDraft({text, id: null})}
            placeholder="Write a message to this agent"
            rows={2}
          />
        </Field>
        <Row gap="space-3" wrap>
          <Button
            text={sending ? 'Sending' : retrying ? 'Try again' : 'Send'}
            variant="primary"
            press={() => {
              const id = idForSend(draft, box, props.newId);
              setDraft({text: shown, id});
              props.send(id, shown);
            }}
            {...(shown.trim() === '' || sending
              ? {disabledReason: sending ? 'Sending.' : 'Write a message first.', quietReason: true}
              : {})}
          />
          {box.status === 'failed' ? <Chip label={failureLine(box.fault)} tone="err" dot /> : null}
          {box.status === 'answered' || box.status === 'failed' ? (
            <Button text="Clear" variant="quiet" size="sm" press={props.dismiss} />
          ) : null}
        </Row>
        {box.status === 'failed' ? (
          <Text tone="muted" variant="small">{`Details: ${box.fault.detail}`}</Text>
        ) : null}
        {props.sent.map(message => (
          <SentMessage
            key={message.id}
            text={message.body.case === 'text' ? message.body.value : ''}
            line={storedLine(message)}
          />
        ))}
        {box.status === 'answered' && !reported ? (
          <SentMessage text={box.text} line={receiptLine(box.receipt)} />
        ) : null}
      </Stack>
    </Card>
  );
}

/** One agent: who it is, every turn it has taken (past and live in one list), and a message box. */
export function AgentPanel(props: {
  readonly state: OrchestratorState;
  readonly agent: Agent;
  readonly name: string;
  /** This agent's own connection, history and message box. */
  readonly view: AgentView;
  readonly showOlder: () => void;
  readonly newId: () => string;
  readonly send: (commandId: string, text: string) => void;
  readonly dismiss: () => void;
  readonly close: () => void;
}): JSX.Element {
  const {state, agent} = props;
  const turns = agentThread(state, agent.id);
  const oldest = oldestOrdinal(state, agent.id);
  const more = oldest !== null && oldest > 1;
  const status = agentWord(agent.status);
  return (
    <Stack gap="space-4">
      <Row gap="space-3" justify="between">
        <Row gap="space-3">
          <Text variant="heading">{props.name}</Text>
          <Chip label={status.label} tone={status.tone} dot />
          <Chip label={agent.model} mono />
        </Row>
        <Button text="Close" variant="quiet" size="sm" press={props.close} />
      </Row>
      <KeyValue
        items={[
          {key: 'Tool', value: harnessWord(agent.harness)},
          {key: 'Effort', value: agent.reasoningEffort === '' ? 'Default' : agent.reasoningEffort},
        ]}
      />
      {more ? (
        <Row gap="space-3">
          <Button
            text="Show earlier steps"
            variant="secondary"
            size="sm"
            press={props.showOlder}
            {...(props.view.older === 'loading'
              ? {disabledReason: 'Loading.', quietReason: true}
              : {})}
          />
          {props.view.olderError === null ? null : (
            <Text tone="muted">{`Could not load earlier steps. ${faultWord(props.view.olderError.kind)}`}</Text>
          )}
        </Row>
      ) : null}
      {turns.length === 0 ? (
        <EmptyState title="Nothing yet" body="This agent has not taken a turn yet." />
      ) : (
        <Thread label={`${props.name}, turn by turn`} items={turns.flatMap(turnItems)} />
      )}
      <MessageBox
        agent={agent}
        box={props.view.box}
        sent={messagesOf(state, agent.id).filter(
          message => message.fromAgentId === '' && message.body.case === 'text',
        )}
        newId={props.newId}
        send={props.send}
        dismiss={props.dismiss}
      />
    </Stack>
  );
}
