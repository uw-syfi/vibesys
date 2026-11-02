import {
  type Agent,
  ItemStatus,
  type Receipt,
  type ToolCall,
  TurnTrigger,
} from '@vibesys/orchestrator-proto';
import {
  agentThread,
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
import {
  agentWord,
  deliveryWord,
  formatDuration,
  harnessWord,
  refusalWord,
  roleWord,
  turnWord,
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
  return {
    kind: 'tool',
    id: item.id,
    name: call.tool,
    state: running ? 'pending' : call.isError ? 'failed' : 'ok',
    summary: call.argsJson.slice(0, 80),
    input: call.argsJson === '' ? null : call.argsJson,
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

function receiptLine(receipt: Receipt): string {
  return receipt.refusal === 0 ? deliveryWord(receipt.delivery) : refusalWord(receipt.refusal);
}

export interface MessageBoxView {
  readonly status: 'idle' | 'sending' | 'answered' | 'failed';
  readonly receipt?: Receipt;
  readonly error?: string;
}

function MessageBox(props: {
  readonly agent: Agent;
  readonly box: MessageBoxView;
  readonly send: (text: string) => void;
  readonly dismiss: () => void;
}): JSX.Element {
  const [text, setText] = useState('');
  const sending = props.box.status === 'sending';
  return (
    <Card section={`Message ${roleWord(props.agent.role)}`}>
      <Stack gap="space-3">
        <Field label="Your message" help={capabilityLine(props.agent)}>
          <TextInput
            value={text}
            change={setText}
            placeholder="Write a message to this agent"
            rows={2}
          />
        </Field>
        <Row gap="space-3">
          <Button
            text={sending ? 'Sending' : 'Send'}
            variant="primary"
            press={() => {
              props.send(text);
              setText('');
            }}
            {...(text.trim() === '' || sending
              ? {disabledReason: sending ? 'Sending.' : 'Write a message first.', quietReason: true}
              : {})}
          />
          {props.box.status === 'answered' && props.box.receipt !== undefined ? (
            <Chip
              label={receiptLine(props.box.receipt)}
              tone={props.box.receipt.refusal === 0 ? 'ok' : 'err'}
              dot
            />
          ) : null}
          {props.box.status === 'failed' ? (
            <Chip
              label={`Could not send: ${props.box.error ?? 'connection lost'}`}
              tone="err"
              dot
            />
          ) : null}
          {props.box.status === 'answered' || props.box.status === 'failed' ? (
            <Button text="Clear" variant="quiet" size="sm" press={props.dismiss} />
          ) : null}
        </Row>
      </Stack>
    </Card>
  );
}

/** One agent: who it is, every turn it has taken (past and live in one list), and a message box. */
export function AgentPanel(props: {
  readonly state: OrchestratorState;
  readonly agent: Agent;
  readonly name: string;
  readonly older: 'idle' | 'loading';
  readonly olderError: string | null;
  readonly box: MessageBoxView;
  readonly showOlder: () => void;
  readonly send: (text: string) => void;
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
            {...(props.older === 'loading' ? {disabledReason: 'Loading.', quietReason: true} : {})}
          />
          {props.olderError === null ? null : <Text tone="muted">{props.olderError}</Text>}
        </Row>
      ) : null}
      {turns.length === 0 ? (
        <EmptyState title="Nothing yet" body="This agent has not taken a turn yet." />
      ) : (
        <Thread label={`${props.name}, turn by turn`} items={turns.flatMap(turnItems)} />
      )}
      <MessageBox agent={agent} box={props.box} send={props.send} dismiss={props.dismiss} />
    </Stack>
  );
}
