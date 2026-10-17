import {type JSX, useState} from 'react';
import {Button} from './button.js';
import {Collapsible} from './collapsible.js';
import {Markdown} from './markdown.js';
import {Code} from './message.js';
import {Chip, type StatusTone} from './status.js';
import {Text} from './text.js';
import {useSlot} from './windowing.js';

/**
 * One field of a structured reply, already read from the reply's own schema: `prose` is text to
 * read, `badge` a choice from a closed set, `fact` a short value, `list` a list of short items and
 * `group` a nested record or a list of records. A field with no value is not given.
 */
export type ReplyField =
  | {readonly kind: 'prose'; readonly key: string; readonly label: string; readonly text: string}
  | {
      readonly kind: 'badge';
      readonly key: string;
      readonly label: string;
      readonly value: string;
      readonly tone?: StatusTone;
    }
  | {readonly kind: 'fact'; readonly key: string; readonly label: string; readonly value: string}
  | {
      readonly kind: 'list';
      readonly key: string;
      readonly label: string;
      readonly items: readonly string[];
    }
  | {
      readonly kind: 'group';
      readonly key: string;
      readonly label: string;
      readonly fields: readonly ReplyField[];
    };

export interface Reading {
  readonly opened: ReadonlySet<string>;
  readonly flip: (path: string) => void;
}

function FieldView(props: {
  readonly field: ReplyField;
  readonly path: string;
  readonly reading: Reading;
}): JSX.Element {
  const {field, path, reading} = props;
  switch (field.kind) {
    case 'prose':
      return (
        <div className="vs-reply-field">
          <Text variant="section">{field.label}</Text>
          <Markdown text={field.text} />
        </div>
      );
    case 'badge':
      return (
        <div className="vs-reply-field" data-inline>
          <Text variant="section">{field.label}</Text>
          <Chip label={field.value} {...(field.tone === undefined ? {} : {tone: field.tone})} />
        </div>
      );
    case 'fact':
      return (
        <div className="vs-reply-field" data-inline>
          <Text variant="section">{field.label}</Text>
          <Text variant="code">{field.value}</Text>
        </div>
      );
    case 'list':
      return (
        <div className="vs-reply-field">
          <Text variant="section">{field.label}</Text>
          <ul className="vs-prose-list">
            {field.items.map((item, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: the items are a fixed list read from one reply, never reordered.
              <li key={index}>
                <Markdown text={item} />
              </li>
            ))}
          </ul>
        </div>
      );
    case 'group':
      return (
        <Collapsible
          label={field.label}
          summary={`${field.fields.length} field${field.fields.length === 1 ? '' : 's'}`}
          open={reading.opened.has(path)}
          toggle={() => reading.flip(path)}
        >
          <ReplyFields fields={field.fields} path={path} reading={reading} />
        </Collapsible>
      );
  }
}

/** The fields of a reply, drawn in order; a group opens as the reader presses it. */
export function ReplyFields(props: {
  readonly fields: readonly ReplyField[];
  readonly path: string;
  readonly reading: Reading;
}): JSX.Element {
  return (
    <div className="vs-reply-fields">
      {props.fields.map(field => (
        <FieldView
          key={field.key}
          field={field}
          path={`${props.path}/${field.key}`}
          reading={props.reading}
        />
      ))}
    </div>
  );
}

/**
 * A reply that came as data: who wrote it, what kind it is (`title`), its choices as chips on the
 * title line, and its other fields below, text as text, lists as lists and nested records as blocks
 * the reader opens. The caller reads the fields from the reply's schema; this draws them. A "Raw"
 * button shows the reply as the JSON it came as (`raw`), for the reader who wants the braces.
 */
export function StructuredReply(props: {
  readonly author: string;
  readonly title: string;
  readonly fields: readonly ReplyField[];
  readonly raw: string;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
  /**
   * The `key`s of the fields to show at first (besides the choices on the title line), with the
   * rest behind "Show all fields"; absent, every field shows.
   */
  readonly overview?: readonly string[];
}): JSX.Element {
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const [raw, setRaw] = useState(false);
  const [all, setAll] = useState(false);
  const flip = (path: string): void => {
    const next = new Set(opened);
    if (!next.delete(path)) next.add(path);
    setOpened(next);
  };
  // In a windowed `Narrative` the reply reports its height, so a tall reply is not taken for a line.
  const slot = useSlot();
  const badges = props.fields.flatMap(field => (field.kind === 'badge' ? [field] : []));
  const fields = props.fields.filter(field => field.kind !== 'badge');
  const full = props.overview === undefined || all;
  const body = full ? fields : fields.filter(field => props.overview?.includes(field.key));
  const hidden = fields.length - body.length;
  return (
    <div ref={slot?.measure} className="vs-structured-reply">
      <Collapsible
        label={props.author}
        leading={
          <>
            <Chip label={props.title} />
            {badges.map(badge => (
              <Chip
                key={badge.key}
                label={badge.value}
                {...(badge.tone === undefined ? {} : {tone: badge.tone})}
              />
            ))}
          </>
        }
        open={props.open}
        toggle={props.toggle}
      >
        {body.length === 0 && badges.length === 0 ? (
          <Text tone="muted">The reply has no fields.</Text>
        ) : (
          <ReplyFields fields={body} path="" reading={{opened, flip}} />
        )}
        {props.overview === undefined || (hidden === 0 && !all) ? null : (
          <span>
            <Button
              size="sm"
              variant="quiet"
              text={all ? 'Show fewer fields' : `Show all fields (${hidden} more)`}
              expanded={all}
              press={() => setAll(!all)}
            />
          </span>
        )}
        {full ? (
          <span>
            <Button
              size="sm"
              variant="quiet"
              text="Raw"
              expanded={raw}
              press={() => setRaw(!raw)}
            />
          </span>
        ) : null}
        {raw && full ? <Code label="Raw" text={props.raw} /> : null}
      </Collapsible>
    </div>
  );
}
