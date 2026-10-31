import {VIBESYS_WORDMARK} from '@vibesys/feature-new-run/renderer';
import {
  ActionPanel,
  Banner,
  BRAND_MARK_NAMES,
  BrandMark,
  Button,
  type ButtonVariant,
  Card,
  Checkbox,
  Chip,
  ChoiceTiles,
  type Completion,
  Field,
  Grid,
  ICON_NAMES,
  Icon,
  PathInput,
  Row,
  SectionLayout,
  SegmentedControl,
  Select,
  Slider,
  Split,
  Stack,
  StatusDot,
  type StatusTone,
  StepList,
  Switch,
  Text,
  TextInput,
  type TextStyle,
  type TextTone,
  Toast,
  type ToastItem,
  ToastStack,
  Wordmark,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const STYLES: readonly TextStyle[] = [
  'title',
  'heading',
  'item',
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
      <Text variant="code" tone="muted">
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

function Tiles(): JSX.Element {
  const [value, setValue] = useState<'claude' | 'codex' | 'other'>('claude');
  return (
    <ChoiceTiles
      label="Agent"
      value={value}
      change={setValue}
      options={[
        {value: 'claude', label: 'Claude Code', leading: <BrandMark mark="claudecode" />},
        {value: 'codex', label: 'Codex', leading: <BrandMark mark={null} />},
        {value: 'other', label: 'OpenCode', leading: <BrandMark mark="opencode" />},
      ]}
    />
  );
}

function ActionPanelDemo(props: {readonly open: boolean; readonly failing: boolean}): JSX.Element {
  const [open, setOpen] = useState(props.open);
  return (
    <ActionPanel
      label={props.failing ? 'Needs attention' : 'Ready'}
      status={
        props.failing
          ? {state: 'fail', text: '1 check needs attention'}
          : {state: 'ok', text: 'Ready · 7 checks passed'}
      }
      open={open}
      toggle={setOpen}
      summary="queue-rs · mpmc · This Mac · Claude Opus 5.5 · rounds as configured"
      details={
        <Text tone="muted">
          {props.failing ? 'Docker is not reachable.' : 'Every check passed.'}
        </Text>
      }
      action={
        props.failing ? (
          <Button
            variant="primary"
            text="Start run"
            disabledReason="Fix the check above."
            press={() => undefined}
          />
        ) : (
          <Button variant="primary" text="Start run" press={() => undefined} />
        )
      }
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

function SliderDemo(): JSX.Element {
  const tiers = ['Luna', 'Sol', 'Astra'];
  const [index, setIndex] = useState(1);
  return (
    <Field label="Tier" help="Drag, or use the arrow keys, to pick a tier.">
      <Slider options={tiers} value={index} change={setIndex} />
    </Field>
  );
}

/** The layout, text and form components (batch 1 of the design system). */
const noop = (): void => undefined;

const SAMPLE_TOASTS: readonly ToastItem[] = [
  {
    id: 'read',
    tone: 'warn',
    title: 'The machines could not be read',
    detail: 'Connection refused.',
    open: false,
  },
  {
    id: 'save',
    tone: 'err',
    title: 'Could not save the change',
    detail: 'The settings file is read-only.',
    open: false,
  },
];

function ToastDemo(): JSX.Element {
  const [open, setOpen] = useState(true);
  return (
    <Stack>
      <Toast
        tone="err"
        title="Could not save the change"
        detail="The settings file is read-only."
        open={open}
        toggle={setOpen}
        dismiss={noop}
      />
      <Toast
        tone="warn"
        title="The machines could not be read"
        detail="Connection refused."
        open={false}
        toggle={noop}
        dismiss={noop}
      />
      <Toast
        tone="err"
        title="The command was not taken"
        open={false}
        toggle={noop}
        dismiss={noop}
      />
    </Stack>
  );
}

/** The stack sits over the window at its top right, so it is shown on request, not over the page. */
function ToastStackDemo(): JSX.Element {
  const [toasts, setToasts] = useState<readonly ToastItem[]>([]);
  return (
    <Stack>
      <Button
        text="Show notifications"
        press={() => setToasts(SAMPLE_TOASTS)}
        {...(toasts.length > 0 ? {disabledReason: 'Shown at the top right of the window'} : {})}
      />
      <ToastStack
        label="Notifications"
        toasts={toasts}
        toggle={(id, open) =>
          setToasts(list => list.map(toast => (toast.id === id ? {...toast, open} : toast)))
        }
        dismiss={id => setToasts(list => list.filter(toast => toast.id !== id))}
      />
    </Stack>
  );
}

export const BASIC_ENTRIES = {
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
    render: () => (
      <Stack>
        <Split main={<Box label="main" />} aside={<Box label="aside" />} />
        <Split main={<Box label="main" />} aside={<Box label="aside first" />} asideFirst />
      </Stack>
    ),
  },
  SectionLayout: {
    render: () => (
      <SectionLayout
        label="Example sections"
        sections={
          <Stack gap="space-1">
            <Text>Appearance</Text>
            <Text>Machines</Text>
            <Text>Notifications</Text>
          </Stack>
        }
      >
        <Box label="Selected section" />
      </SectionLayout>
    ),
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
          <Text key={style} variant={style}>
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
  Wordmark: {
    render: () => (
      <Stack>
        <Wordmark art={VIBESYS_WORDMARK} label="VibeSys" align="center" tone="accent" />
        <Wordmark art={VIBESYS_WORDMARK} label="VibeSys, as a gradient" tone="gradient" />
        <Wordmark art={VIBESYS_WORDMARK} size="compact" />
      </Stack>
    ),
  },
  Icon: {
    render: () => (
      <Row gap="space-4" wrap>
        {ICON_NAMES.map(name => (
          <Row key={name} gap="space-2">
            <Icon name={name} />
            <Text variant="code" tone="muted">
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
          <Row key={variant} gap="space-3" wrap>
            <Button variant={variant} text={variant} press={() => undefined} />
            <Button variant={variant} size="sm" text="small" press={() => undefined} />
            <Button variant={variant} icon="plus" text="With icon" press={() => undefined} />
            <Button variant={variant} icon="more" label="More actions" press={() => undefined} />
          </Row>
        ))}
        <Row gap="space-3" wrap align="start">
          {VARIANTS.map(variant => (
            <Button
              key={variant}
              variant={variant}
              text="Disabled"
              press={() => undefined}
              disabledReason="Choose a folder first."
            />
          ))}
        </Row>
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
  ActionPanel: {
    render: () => (
      <Stack>
        <ActionPanelDemo open={false} failing={false} />
        <ActionPanelDemo open failing />
      </Stack>
    ),
  },
  ChoiceTiles: {render: () => <Tiles />},
  BrandMark: {
    render: () => (
      <Row gap="space-5">
        {BRAND_MARK_NAMES.map(mark => (
          <BrandMark key={mark} mark={mark} />
        ))}
        <BrandMark mark={null} />
      </Row>
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
        <Field label="Several lines">
          <TextInput
            value={'Keep the weights in bf16.\nCheck the scale first.'}
            rows={3}
            change={() => undefined}
          />
        </Field>
      </Grid>
    ),
  },
  PathInput: {render: () => <PathDemo />},
  Slider: {render: () => <SliderDemo />},
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
  StepList: {
    render: () => (
      <Stack>
        <StepList
          title="This Mac"
          steps={[
            {id: 'gateway', label: 'Starting gateway', state: 'done', position: '1/3'},
            {id: 'handshake', label: 'Connecting', state: 'current', position: '2/3'},
            {id: 'runs', label: 'Loading runs', state: 'pending', position: '3/3'},
          ]}
        />
        <StepList
          title="host-a"
          steps={[
            {
              id: 'gateway',
              label: 'Starting gateway',
              state: 'failed',
              position: '1/3',
              note: 'gateway exited (code 1)',
            },
            {id: 'handshake', label: 'Connecting', state: 'pending', position: '2/3'},
            {id: 'runs', label: 'Loading runs', state: 'pending', position: '3/3'},
          ]}
          retry={() => undefined}
        />
      </Stack>
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
          tone="failed"
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
  Toast: {render: () => <ToastDemo />},
  ToastStack: {render: () => <ToastStackDemo />},
} satisfies Partial<Record<ComponentName, GalleryEntry>>;
