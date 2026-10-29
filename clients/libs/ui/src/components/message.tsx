import {type JSX, useState} from 'react';
import {Button} from './button.js';
import {Collapsible} from './collapsible.js';
import {Markdown} from './markdown.js';
import {markdownToPlain} from './markdown-plain.js';
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

/** The first non-empty line of `text` as plain words, to preview a closed message. */
function firstLine(text: string): string {
  return markdownToPlain(text.split('\n').find(line => line.trim() !== '') ?? '');
}

/**
 * One message: who wrote it (`author`), what kind it is, and its text, which the reader opens
 * and closes. Closed, it shows the text's first line as plain words. A `reply` or `reasoning`
 * is Markdown; a `prompt` or `note` is plain text with its line breaks kept. `notice` is a caveat
 * about it, such as that it was cut short.
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
  // A reply and its reasoning are written text, read as Markdown; a prompt and a note are not.
  const written = props.variant === 'reply' || props.variant === 'reasoning';
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
          <div className="vs-message-text" data-markdown={written}>
            {written ? <Markdown text={props.text} /> : props.text}
          </div>
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

/** Lines of a long text shown before "Show more". */
const VISIBLE_LINES = 20;
const VISIBLE_CHARS = 3000;

/** The start of `text` that is shown at first, and how many lines the rest holds. */
function abridged(text: string): {readonly shown: string; readonly hiddenLines: number} {
  const lines = text.split('\n');
  if (lines.length <= VISIBLE_LINES && text.length <= VISIBLE_CHARS) {
    return {shown: text, hiddenLines: 0};
  }
  const shown = lines.slice(0, VISIBLE_LINES).join('\n').slice(0, VISIBLE_CHARS);
  return {shown, hiddenLines: Math.max(1, lines.length - shown.split('\n').length)};
}

/** A block of text on the log background; a long one shows its start, then all on request. */
export function Code(props: {readonly label: string; readonly text: string}): JSX.Element {
  const [all, setAll] = useState(false);
  const {shown, hiddenLines} = abridged(props.text);
  return (
    <div className="vs-code-block">
      <Text variant="section">{props.label}</Text>
      <pre
        className="vs-code-block-body"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
        tabIndex={0}
      >
        {all || hiddenLines === 0 ? props.text : `${shown}\n…`}
      </pre>
      {all || hiddenLines === 0 ? null : (
        <span>
          <Button
            size="sm"
            variant="quiet"
            text={`Show more (${hiddenLines} more line${hiddenLines === 1 ? '' : 's'})`}
            press={() => setAll(true)}
          />
        </span>
      )}
    </div>
  );
}

/**
 * One call of a tool: its `name`, its state, the `input` it was given and the `output` it
 * returned, each shown as plain text when the reader opens the call. `input` is null for a call
 * that took none, and `output` is null until the result arrives. Closed, it is one line: the
 * name, a `summary` of its main argument, and `meta` (the result's size, or why it failed). A long
 * output shows its start and the rest on request.
 */
export function ToolCall(props: {
  readonly name: string;
  readonly state: ToolState;
  readonly summary?: string;
  readonly meta?: string;
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
        {...(props.summary === undefined ? {} : {summary: props.summary})}
        {...(props.meta === undefined ? {} : {meta: props.meta})}
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
