import type {ChatSteerProposed} from '@vibesys/runs/chat';
import {Button, Card, ChatMessage, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ChatHistory} from '../../state/index.js';
export function SessionPane(props: {
  readonly close: () => void;
  readonly history: ChatHistory;
  readonly text: string;
  readonly edit: (text: string) => void;
  readonly send: () => void;
  readonly retry: () => void;
  readonly busy: boolean;
  readonly error: string | null;
  readonly uncertain: boolean;
  readonly steerBusy: boolean;
  readonly steer: (proposal: ChatSteerProposed) => void;
}): JSX.Element {
  return (
    <Card>
      <section aria-label="Chat">
        <Stack gap="space-3">
          <Row justify="between">
            <Text>Chat</Text>
            <Button text="Close chat" press={props.close} />
          </Row>
          <div role="log" aria-label="Messages" aria-live="polite">
            <Stack gap="space-3">
              {props.history.turns.map(turn => (
                <Stack key={turn.requestId} gap="space-1">
                  <ChatMessage side="end" text={turn.question} />
                  <ChatMessage
                    side="start"
                    text={turn.answer}
                    {...(turn.failed || turn.streaming
                      ? {notice: turn.failed ? 'Failed' : 'Answering'}
                      : {})}
                  />
                </Stack>
              ))}
            </Stack>
          </div>
          {props.history.proposals.map(proposal => (
            <Card key={proposal.id}>
              <Stack gap="space-2">
                <Text>Proposed steer</Text>
                <Text>{proposal.text}</Text>
                <Button
                  text="Send as steer"
                  press={() => props.steer(proposal)}
                  {...(props.steerBusy ? {disabledReason: 'Sending'} : {})}
                />
              </Stack>
            </Card>
          ))}
          <Field label="Message">
            <TextInput rows={3} value={props.text} change={props.edit} />
          </Field>
          <Row gap="space-2">
            <Button
              quietReason
              text={props.busy ? 'Sending' : 'Send'}
              press={props.send}
              {...(props.busy || !props.text.trim()
                ? {disabledReason: props.busy ? 'Sending' : 'Enter a message'}
                : {})}
            />
            {props.error ? (
              <Text tone="err" title={props.error}>
                Not confirmed
              </Text>
            ) : null}
            {props.uncertain ? (
              <Button
                text="Retry message"
                press={props.retry}
                {...(props.busy ? {disabledReason: 'Sending'} : {})}
              />
            ) : null}
          </Row>
        </Stack>
      </section>
    </Card>
  );
}
