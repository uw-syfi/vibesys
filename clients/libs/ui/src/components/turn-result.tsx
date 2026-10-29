import {type JSX, useState} from 'react';
import {Button} from './button.js';
import {Card} from './card.js';
import {Row, Stack} from './layout.js';
import {Code} from './message.js';
import {Chip, type StatusTone} from './status.js';
import {type ReplyField, ReplyFields} from './structured-reply.js';
import {Text} from './text.js';
import {useSlot} from './windowing.js';

/**
 * What a turn ends with, laid out to be reviewed: a `label` and the author, a large `headline`
 * (a plan's idea title), its choices as chips, a `context` line (what it builds on, a link when it
 * can be opened), then `sections` in reading order, text in full (wrapped, never cut) and lists as
 * lists, then every field of `more` after them, with a "Raw" button for the reply as the JSON it
 * came as. The caller decides which fields lead; this draws them.
 */
export function TurnResult(props: {
  readonly label: string;
  readonly author: string;
  readonly headline?: string;
  readonly chips?: readonly {readonly label: string; readonly tone?: StatusTone}[];
  readonly context?: {
    readonly prefix: string;
    readonly label: string;
    /** Opens what the label names; absent when it names nothing that can open. */
    readonly press?: () => void;
  };
  /** The body's `prose` and `list` fields, in the order they read. */
  readonly sections: readonly ReplyField[];
  readonly more: readonly ReplyField[];
  readonly raw: string;
}): JSX.Element {
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const [raw, setRaw] = useState(false);
  const flip = (path: string): void => {
    const next = new Set(opened);
    if (!next.delete(path)) next.add(path);
    setOpened(next);
  };
  // In a windowed `Narrative` the card reports its height, so a tall card is not taken for a line.
  const slot = useSlot();
  const {context} = props;
  return (
    <div ref={slot?.measure} className="vs-turn-result">
      <Card>
        <Stack gap="space-4">
          <Row gap="space-3" wrap>
            <Text variant="section">{props.label}</Text>
            {(props.chips ?? []).map(chip => (
              <Chip
                key={chip.label}
                label={chip.label}
                {...(chip.tone === undefined ? {} : {tone: chip.tone})}
              />
            ))}
            <Text variant="small" tone="muted">
              {props.author}
            </Text>
          </Row>
          {props.headline === undefined ? null : (
            <Text variant="title" as="h3">
              {props.headline}
            </Text>
          )}
          {context === undefined ? null : (
            <Row gap="space-2" wrap>
              <Text variant="section">{context.prefix}</Text>
              {context.press === undefined ? (
                <Text variant="code">{context.label}</Text>
              ) : (
                <Button size="sm" variant="quiet" text={context.label} press={context.press} />
              )}
            </Row>
          )}
          <ReplyFields fields={props.sections} path="" reading={{opened, flip}} />
          {props.more.length === 0 ? null : (
            <ReplyFields fields={props.more} path="more" reading={{opened, flip}} />
          )}
          <span>
            <Button
              size="sm"
              variant="quiet"
              text="Raw"
              expanded={raw}
              press={() => setRaw(!raw)}
            />
          </span>
          {raw ? <Code label="Raw" text={props.raw} /> : null}
        </Stack>
      </Card>
    </div>
  );
}
