import {
  type Agent,
  AgentStatus,
  Delivery,
  type Message,
  type Receipt,
} from '@vibesys/orchestrator-proto';
import {Button, Card, Chip, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {type CommandProgress, messageIs, ms} from '../../state/index.js';
import {useDisplayClock} from '../display-clock.js';
import {CommandLine} from '../orchestrator/command-line.js';
import {
  deliveryWord,
  harnessWord,
  refusalWord,
  rejectionWord,
  roleWord,
  type Word,
} from '../orchestrator/words.js';

/** An agent session the host will refuse a new message for. */
const FINAL: ReadonlySet<AgentStatus> = new Set([
  AgentStatus.ENDED,
  AgentStatus.FAILED,
  AgentStatus.LOST,
]);

/** What this agent's tool lets a message do, as a label. */
function capabilityLine(agent: Agent): string {
  const takes = agent.capabilities?.steers === true ? 'reaches it mid-turn' : 'waits for its step';
  return `${harnessWord(agent.harness)}: ${takes}`;
}

/** What a stored message's delivery says now, with the reason when it was rejected. */
function storedLine(message: Message): {label: string; tone: Word['tone']} {
  const word = deliveryWord(message.delivery);
  const why =
    message.delivery === Delivery.REJECTED ? ` \u00b7 ${rejectionWord(message.rejection)}` : '';
  return {label: `${word.label}${why}`, tone: word.tone};
}

/** The line for an answer whose message the host has not reported yet: the receipt's own words. */
function receiptLine(receipt: Receipt): {label: string; tone: Word['tone']} {
  if (receipt.refusal !== 0) return {label: refusalWord(receipt.refusal), tone: 'err'};
  const word = deliveryWord(receipt.delivery);
  return {label: word.label, tone: word.tone};
}

function SentMessage(props: {
  readonly text: string;
  readonly atMs: number | null;
  readonly line: {readonly label: string; readonly tone: Word['tone']};
}): JSX.Element {
  const clock = useDisplayClock();
  return (
    <Row gap="space-3" wrap>
      <Text tone="muted">{props.text}</Text>
      <Chip label={props.line.label} tone={props.line.tone} dot />
      {props.atMs === null ? null : <Text variant="small">{clock.time24(props.atMs)}</Text>}
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
        <SentMessage atMs={props.pendingAtMs} text="Last message" line={receiptLine(pending)} />
      )}
    </>
  );
}

/** The box a person writes to one agent in, and what became of each message they sent it. */
export function MessageBox(props: {
  readonly nowMs: number;
  readonly agent: Agent;
  /** What the last press of Send came to, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  /** The messages the person sent to this agent, as the host last reported them. */
  readonly sent: readonly Message[];
  readonly send: (text: string) => void;
  readonly retry: () => void;
}): JSX.Element | null {
  const [draft, setDraft] = useState('');
  const [sentAtMs, setSentAtMs] = useState<number | null>(null);
  const [sentText, setSentText] = useState<string | null>(null);
  const {progress} = props;
  const canSend = !FINAL.has(props.agent.status);
  const sending = progress?._tag === 'Sending';
  // The text stays until the host has taken the message; only then is the box empty again.
  const taken = progress?._tag === 'Accepted' && sentText === draft;
  const shown = taken ? '' : draft;
  // Keep past messages and an in-flight result visible after the session ends. With neither, an
  // empty card would only advertise an action the host is guaranteed to refuse.
  if (!canSend && props.sent.length === 0 && progress === undefined) return null;
  return (
    <Card section={`${canSend ? 'Message' : 'Messages to'} ${roleWord(props.agent.role)}`}>
      <Stack gap="space-3">
        {canSend ? (
          <>
            <Field label="Your message">
              <TextInput
                value={shown}
                change={setDraft}
                placeholder="Write a message to this agent"
                rows={2}
              />
            </Field>
            <Row>
              <Button
                text={sending ? 'Sending message' : 'Send message'}
                label="Send message"
                hint={capabilityLine(props.agent)}
                variant="primary"
                press={() => {
                  setDraft(shown);
                  setSentText(shown);
                  setSentAtMs(props.nowMs);
                  props.send(shown);
                }}
                {...(shown.trim() === '' || sending
                  ? {
                      disabledReason: sending ? 'Sending' : 'Write a message first',
                      quietReason: true,
                    }
                  : {})}
              />
            </Row>
          </>
        ) : null}
        <CommandLine progress={progress} done="Send message" retry={props.retry} />
        <SentMessages pendingAtMs={sentAtMs} sent={props.sent} progress={progress} />
      </Stack>
    </Card>
  );
}
