import type * as ui from '@vibesys/ui';
import {
  Banner,
  Button,
  type ButtonVariant,
  Card,
  Checkbox,
  Chip,
  type Completion,
  Field,
  Grid,
  ICON_NAMES,
  Icon,
  PathInput,
  Row,
  SegmentedControl,
  Select,
  Split,
  Stack,
  StatusDot,
  type StatusTone,
  Switch,
  Text,
  TextInput,
  type TextStyle,
  type TextTone,
  Wordmark,
} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import {WORDMARK} from './wordmark.js';

/** The names of the components `@vibesys/ui`'s `.` entry exports: its capitalized functions. */
export type ComponentName = {
  [K in keyof typeof ui]: (typeof ui)[K] extends (...args: never[]) => unknown
    ? K extends Capitalize<K>
      ? K
      : never
    : never;
}[keyof typeof ui];

/** One component's gallery entry: every state it has, in fixed content. */
export interface GalleryEntry {
  readonly render: () => ReactNode;
}

const STYLES: readonly TextStyle[] = [
  'title',
  'heading',
  'body',
  'body-strong',
  'small',
  'caption',
  'code',
  'section',
  'metric',
];
const TEXT_TONES: readonly TextTone[] = ['default', 'muted', 'ok', 'warn', 'err'];
const STATUS_TONES: readonly StatusTone[] = ['neutral', 'live', 'ok', 'warn', 'err'];
const VARIANTS: readonly ButtonVariant[] = ['primary', 'secondary', 'quiet', 'danger'];
const COMPLETIONS: readonly Completion[] = [
  {value: '~/src/queue', detail: '0.1.1'},
  {value: '~/work/queue', detail: '0.1.0'},
];

function Box(props: {readonly label: string}): JSX.Element {
  return (
    <Card>
      <Text style="code" tone="muted">
        {props.label}
      </Text>
    </Card>
  );
}

function Segments(props: {readonly size: 'sm' | 'md'}): JSX.Element {
  const [value, setValue] = useState<'recent' | 'folder' | 'example'>('recent');
  return (
    <SegmentedControl
      label={`Source (${props.size})`}
      size={props.size}
      value={value}
      change={setValue}
      options={[
        {value: 'recent', label: 'Recent'},
        {value: 'folder', label: 'Folder'},
        {value: 'example', label: 'Example'},
      ]}
    />
  );
}

function SelectDemo(props: {
  readonly mono: boolean;
  readonly note: {readonly help: string} | {readonly error: string};
}): JSX.Element {
  const [value, setValue] = useState<'a' | 'b' | 'c'>('a');
  return (
    <Field label={props.mono ? 'Model' : 'Size'} {...props.note}>
      <Select
        mono={props.mono}
        value={value}
        change={setValue}
        options={[
          {value: 'a', label: props.mono ? 'model-large-2' : 'Small'},
          {value: 'b', label: props.mono ? 'model-small-2' : 'Medium'},
          {value: 'c', label: 'Large', disabledReason: 'needs more memory'},
        ]}
      />
    </Field>
  );
}

function PathDemo(): JSX.Element {
  const [value, setValue] = useState('~/src/');
  return (
    <Field label="Folder" help="Type a path, or take one of the completions.">
      <PathInput value={value} change={setValue} completions={COMPLETIONS} />
    </Field>
  );
}

function Toggles(props: {readonly kind: 'checkbox' | 'switch'}): JSX.Element {
  const [on, setOn] = useState(true);
  const [off, setOff] = useState(false);
  const Control = props.kind === 'checkbox' ? Checkbox : Switch;
  return (
    <Stack gap="space-1">
      <Control label="Notify me when it finishes" checked={on} change={setOn} />
      <Control label="Play a sound" checked={off} change={setOff} />
      <Control
        label="Show in the menu bar"
        checked={false}
        change={() => undefined}
        disabledReason="Not available on this system."
      />
    </Stack>
  );
}

/** Every public component, keyed by its exported name. */
export const REGISTRY: Readonly<Record<ComponentName, GalleryEntry>> = {
  Page: {
    render: () => (
      <Text tone="muted">
        This gallery is drawn inside a Page of width wide: the page gutter, and content centred at
        its max width.
      </Text>
    ),
  },
  Stack: {
    render: () => (
      <Row gap="space-6" align="start">
        <Stack gap="space-2">
          <Box label="space-2" />
          <Box label="space-2" />
        </Stack>
        <Stack gap="space-5">
          <Box label="space-5" />
          <Box label="space-5" />
        </Stack>
      </Row>
    ),
  },
  Row: {
    render: () => (
      <Stack>
        <Row gap="space-3">
          <Box label="start" />
          <Box label="space-3" />
        </Row>
        <Row justify="between">
          <Box label="between" />
          <Box label="between" />
        </Row>
        <Row gap="space-3" wrap>
          <Box label="wrap" />
          <Stack grow>
            <Box label="grow" />
          </Stack>
        </Row>
      </Stack>
    ),
  },
  Split: {
    render: () => <Split main={<Box label="main" />} aside={<Box label="aside" />} />,
  },
  Grid: {
    render: () => (
      <Stack>
        {(['field', 'tile', 'swatch'] as const).map(min => (
          <Grid key={min} min={min}>
            {[1, 2, 3, 4].map(cell => (
              <Box key={cell} label={`${min} ${cell}`} />
            ))}
          </Grid>
        ))}
      </Stack>
    ),
  },
  Text: {
    render: () => (
      <Stack gap="space-2">
        {STYLES.map(style => (
          <Text key={style} style={style}>
            {style}: The quick brown fox 0123456789
          </Text>
        ))}
        <Row gap="space-4">
          {TEXT_TONES.map(tone => (
            <Text key={tone} tone={tone}>
              {tone}
            </Text>
          ))}
        </Row>
      </Stack>
    ),
  },
  Wordmark: {render: () => <Wordmark art={WORDMARK} label="VibeSys" />},
  Icon: {
    render: () => (
      <Row gap="space-4" wrap>
        {ICON_NAMES.map(name => (
          <Row key={name} gap="space-2">
            <Icon name={name} />
            <Text style="code" tone="muted">
              {name}
            </Text>
          </Row>
        ))}
      </Row>
    ),
  },
  Button: {
    render: () => (
      <Stack>
        {VARIANTS.map(variant => (
          <Row key={variant} gap="space-3" wrap align="start">
            <Button variant={variant} text={variant} press={() => undefined} />
            <Button variant={variant} size="sm" text="small" press={() => undefined} />
            <Button variant={variant} icon="plus" text="With icon" press={() => undefined} />
            <Button variant={variant} icon="more" label="More actions" press={() => undefined} />
            <Button
              variant={variant}
              text="Disabled"
              press={() => undefined}
              disabledReason="Choose a folder first."
            />
          </Row>
        ))}
        <Row gap="space-3">
          <Button text="Link" icon="external" href="#button" />
          <Button text="Show details" expanded={false} variant="quiet" press={() => undefined} />
        </Row>
        <Button variant="primary" full text="Full width" press={() => undefined} />
      </Stack>
    ),
  },
  SegmentedControl: {
    render: () => (
      <Stack>
        <Segments size="md" />
        <Segments size="sm" />
      </Stack>
    ),
  },
  Select: {
    render: () => (
      <Grid min="field">
        <SelectDemo mono={false} note={{help: 'Only tested options are listed.'}} />
        <SelectDemo mono note={{help: 'Shown in mono.'}} />
        <SelectDemo mono={false} note={{error: 'Pick a size that fits.'}} />
      </Grid>
    ),
  },
  Field: {
    render: () => (
      <Grid min="field">
        <Field label="Name" help="Shown in the sidebar.">
          <TextInput value="" placeholder="queue-bench" change={() => undefined} />
        </Field>
        <Field label="Copied to">
          <TextInput value="~/work/queue" mono readOnly />
        </Field>
        <Field label="Extra arguments" error="Unknown flag --fast.">
          <TextInput value="--fast" mono change={() => undefined} />
        </Field>
      </Grid>
    ),
  },
  TextInput: {
    render: () => (
      <Grid min="field">
        <Field label="Plain">
          <TextInput value="Some text" change={() => undefined} />
        </Field>
        <Field label="Mono">
          <TextInput value="/usr/local/bin" mono change={() => undefined} />
        </Field>
        <Field label="Read only">
          <TextInput value="~/work/queue" mono readOnly />
        </Field>
      </Grid>
    ),
  },
  PathInput: {render: () => <PathDemo />},
  Checkbox: {render: () => <Toggles kind="checkbox" />},
  Switch: {render: () => <Toggles kind="switch" />},
  Chip: {
    render: () => (
      <Stack gap="space-3">
        <Row gap="space-3" wrap>
          {STATUS_TONES.map(tone => (
            <Chip key={tone} tone={tone} label={tone} />
          ))}
        </Row>
        <Row gap="space-3" wrap>
          {STATUS_TONES.map(tone => (
            <Chip key={tone} tone={tone} dot label={tone} />
          ))}
        </Row>
      </Stack>
    ),
  },
  StatusDot: {
    render: () => (
      <Row gap="space-5" wrap>
        {STATUS_TONES.map(tone => (
          <Row key={tone} gap="space-3">
            <StatusDot tone={tone} label={tone} />
            <Text>{tone}</Text>
          </Row>
        ))}
      </Row>
    ),
  },
  Card: {
    render: () => (
      <Grid min="tile">
        <Card section="01 · Section">
          <Text>A card with a section title.</Text>
        </Card>
        <Card>
          <Text>A card without a title.</Text>
        </Card>
        <Card flush>
          <Text>A flush card: no padding.</Text>
        </Card>
      </Grid>
    ),
  },
  Banner: {
    render: () => (
      <Stack>
        <Banner title="A neutral message." body="With a body that explains it." />
        <Banner tone="ok" title="It finished." />
        <Banner
          tone="warn"
          title="This folder is inside another Git repository."
          body="Each one is kept in its own repository."
          actions={<Button text="Copy it out" press={() => undefined} />}
        />
        <Banner
          tone="err"
          title="It stopped with an error."
          body="The log has the details."
          actions={
            <>
              <Button variant="primary" text="Try again" press={() => undefined} />
              <Button variant="quiet" text="Dismiss" press={() => undefined} />
            </>
          }
        />
      </Stack>
    ),
  },
};
