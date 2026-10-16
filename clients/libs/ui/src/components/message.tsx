import type {JSX} from 'react';
import {Collapsible} from './collapsible.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** What a message is: a spoken `reply`, the thinking behind one, the `prompt` it answers, or a `note`. */
export type MessageVariant = 'reply' | 'reasoning' | 'prompt' | 'note';

/** The tone a note reads in; replies, reasoning and prompts are `default`. */
export type MessageTone = 'default' | 'ok' | 'err';

const VARIANT_WORDS: Readonly<Record<MessageVariant, string | null>> = {
  reply: null,
  reasoning: 'Reasoning',
  prompt: 'Prompt',
  note: null,
};

const CHIP_TONE: Readonly<Record<MessageTone, StatusTone>> = {
  default: 'neutral',
  ok: 'ok',
  err: 'err',
};

/** The first non-empty line of `text`, to preview a closed message. */
function firstLine(text: string): string {
  return text.split('\n').find(line => line.trim() !== '') ?? '';
}

/**
 * One message: who wrote it (`author`), what kind it is, and its text, which the reader opens
 * and closes. Closed, it shows the text's first line. The text is plain, never markup, with its
 * line breaks kept; `notice` is a caveat about it, such as that it was cut short.
 */
export function Message(props: {
  readonly author: string;
  readonly variant: MessageVariant;
  readonly tone?: MessageTone;
  readonly text: string;
  readonly notice?: string;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const tone = props.tone ?? 'default';
  const words = VARIANT_WORDS[props.variant];
  const leading =
    words !== null ? (
      <Chip label={words} tone={CHIP_TONE[tone]} />
    ) : tone === 'default' ? undefined : (
      <StatusDot tone={CHIP_TONE[tone]} label={tone === 'ok' ? 'Succeeded' : 'Failed'} />
    );
  const preview = firstLine(props.text);
  return (
    <div className="vs-message" data-variant={props.variant} data-tone={tone}>
      <Collapsible
        label={props.author}
        {...(props.open || preview === '' ? {} : {summary: preview})}
        {...(leading === undefined ? {} : {leading})}
        open={props.open}
        toggle={props.toggle}
      >
        {props.text === '' ? (
          <Text tone="muted">Nothing was written.</Text>
        ) : (
          <div className="vs-message-text">{props.text}</div>
        )}
        {props.notice === undefined ? null : (
          <Text variant="small" tone="warn">
            {props.notice}
          </Text>
        )}
      </Collapsible>
    </div>
  );
}

/** Where a tool call stands: `pending` until its result arrives, then `ok` or `failed`. */
export type ToolState = 'pending' | 'ok' | 'failed';

const TOOL_DOTS: Readonly<Record<ToolState, {readonly tone: StatusTone; readonly word: string}>> = {
  pending: {tone: 'live', word: 'In progress'},
  ok: {tone: 'ok', word: 'Done'},
  failed: {tone: 'err', word: 'Failed'},
};

function Code(props: {readonly label: string; readonly text: string}): JSX.Element {
  return (
    <div className="vs-code-block">
      <Text variant="section">{props.label}</Text>
      <pre
        className="vs-code-block-body"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
        tabIndex={0}
      >
        {props.text}
      </pre>
    </div>
  );
}

/**
 * One call of a tool: its `name`, its state, the `input` it was given and the `output` it
 * returned, each shown as plain text when the reader opens the call. `input` is null for a call
 * that took none, and `output` is null until the result arrives. `summary` is a one-line preview
 * shown while the call is closed.
 */
export function ToolCall(props: {
  readonly name: string;
  readonly state: ToolState;
  readonly summary?: string;
  readonly input: string | null;
  readonly output: string | null;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const dot = TOOL_DOTS[props.state];
  return (
    <div className="vs-tool-call" data-state={props.state}>
      <Collapsible
        label={props.name}
        mono
        {...(props.open || props.summary === undefined ? {} : {summary: props.summary})}
        leading={<StatusDot tone={dot.tone} label={dot.word} />}
        open={props.open}
        toggle={props.toggle}
      >
        {props.input === null ? null : <Code label="Input" text={props.input} />}
        {props.output === null ? (
          <Text tone="muted">
            {props.state === 'pending' ? 'Waiting for the result…' : 'No result was recorded.'}
          </Text>
        ) : props.output === '' ? (
          <Text tone="muted">The call returned nothing.</Text>
        ) : (
          <Code label={props.state === 'failed' ? 'Error' : 'Result'} text={props.output} />
        )}
      </Collapsible>
    </div>
  );
}
