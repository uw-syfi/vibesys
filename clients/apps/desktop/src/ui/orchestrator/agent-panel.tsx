import {type Agent, Delivery, type Message, type Receipt} from '@vibesys/orchestrator-proto';
import {Breadcrumb, Button, Card, Chip, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {
  agentNames,
  agentPlace,
  messageIs,
  messagesOf,
  ms,
  type OrchestratorState,
} from '../../state/index.js';
import {AgentTranscript, type HistoryAccess} from '../agents/agent-transcript.js';
import {agentLabel} from '../agents/labels.js';
import {formatUsd} from '../agents/time.js';
import {CommandLine} from './command-line.js';
import {
  agentWord,
  deliveryWord,
  formatClock,
  harnessWord,
  refusalWord,
  rejectionWord,
  roleWord,
  type Word,
} from './words.js';

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
          text={messageIs(message, 'text') ? message.body.value : ''}
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

/** One agent: where it is in the tree, who it is, its session as a conversation, and a message box. */
export function AgentPanel(props: {
  readonly state: OrchestratorState;
  readonly nowMs: number;
  readonly agent: Agent;
  /** What the machine the run is on is called. */
  readonly host: string;
  /** What the last press of Send to this agent came to, or undefined before any. */
  readonly sendProgress: CommandProgress | undefined;
  readonly history: HistoryAccess;
  readonly send: (text: string) => void;
  readonly retrySend: () => void;
  /** Opens another agent of the run. */
  readonly open: (agentId: string) => void;
  readonly close: () => void;
}): JSX.Element {
  const {state, agent} = props;
  const names = agentNames(state, agent.runId);
  const name = names.get(agent.id);
  const label = name === undefined ? 'An agent' : agentLabel(name);
  const status = agentWord(agent.status);
  const cost = formatUsd(agent.usage?.costUsd ?? null);
  const turns = Object.values(state.turns).filter(turn => turn.agentId === agent.id).length;
  const place = agentPlace(state, agent.id);
  return (
    <Stack gap="space-4">
      <Row gap="space-3" justify="between">
        <Breadcrumb
          label="Place in the spawn tree"
          items={place.map(crumb => ({
            id: crumb.agentId,
            label: agentLabel(crumb.name),
            ...(crumb.agentId === agent.id ? {} : {press: () => props.open(crumb.agentId)}),
          }))}
        />
        <Button text="Close agent view" variant="quiet" size="sm" press={props.close} />
      </Row>
      <Row gap="space-3" wrap>
        <Text variant="heading">{label}</Text>
        <Chip label={status.label} tone={status.tone} dot />
        <Chip label={props.host} mono />
        <Chip label={harnessWord(agent.harness)} />
        <Chip label={agent.model} mono />
        {cost === '' ? null : (
          <Text variant="small" tone="muted">{`${cost} · ${turns} turns`}</Text>
        )}
      </Row>
      <MessageBox
        nowMs={props.nowMs}
        agent={agent}
        progress={props.sendProgress}
        sent={messagesOf(state, agent.id).filter(
          message => message.fromAgentId === '' && messageIs(message, 'text'),
        )}
        send={props.send}
        retry={props.retrySend}
      />
      <AgentTranscript
        state={state}
        agentId={agent.id}
        host={props.host}
        nowMs={props.nowMs}
        history={props.history}
        open={props.open}
      />
    </Stack>
  );
}
