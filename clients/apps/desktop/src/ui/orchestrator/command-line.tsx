import {Button, Chip, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {faultText, receiptRefusalWord} from './words.js';

type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

interface CommandWords {
  readonly label: string;
  readonly tone: Tone;
  /** What the host or the transport said, shown verbatim to the one who can fix it. */
  readonly details: string | null;
  /** The person may press the same action again: the same command goes out under the same id. */
  readonly canRetry: boolean;
}

/** How a command stands, in words. `done` names what was asked ("Stop", "Start"). */
function commandWords(progress: CommandProgress, done: string): CommandWords {
  switch (progress._tag) {
    case 'Sending':
      return {label: `${done}: sending.`, tone: 'live', details: null, canRetry: false};
    case 'Accepted':
      return {label: `${done}: accepted by the host.`, tone: 'ok', details: null, canRetry: false};
    case 'Refused':
      return {
        label: `${done}: ${receiptRefusalWord(progress.receipt)}`,
        tone: 'err',
        details: null,
        canRetry: false,
      };
    case 'Unconfirmed':
      return {
        label: `${done}: not confirmed. ${faultText(progress.fault)} It may still have gone through.`,
        tone: 'warn',
        details: progress.fault.detail,
        canRetry: true,
      };
    case 'Failed':
      return {
        label: `${done}: not done. ${faultText(progress.fault)}`,
        tone: 'err',
        details: progress.fault.detail,
        canRetry: progress.fault.retryable,
      };
  }
}

/** One command's outcome as a line under the control that issued it. Nothing before it is issued. */
export function CommandLine(props: {
  readonly progress: CommandProgress | undefined;
  readonly done: string;
  readonly retry: () => void;
}): JSX.Element | null {
  if (props.progress === undefined) return null;
  const words = commandWords(props.progress, props.done);
  return (
    <div role="status" aria-live="polite" aria-label={`${props.done}: outcome`}>
      <Row gap="space-3" wrap>
        <Chip label={words.label} tone={words.tone} dot />
        {words.canRetry ? (
          <Button text="Try again" variant="secondary" size="sm" press={props.retry} />
        ) : null}
      </Row>
      {words.details === null ? null : (
        <Text tone="muted" variant="small">{`Details: ${words.details}`}</Text>
      )}
    </div>
  );
}
