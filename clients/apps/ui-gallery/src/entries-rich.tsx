import {
  AppFrame,
  Button,
  Card,
  Chart,
  type CheckItem,
  CheckList,
  Dialog,
  EmptyState,
  Grid,
  KeyValue,
  type LogLine,
  LogPane,
  Menu,
  type MenuItem,
  Prose,
  Row,
  Scrubber,
  SidebarItem,
  SidebarSection,
  Stack,
  type Stage,
  Stat,
  Table,
  type TableColumn,
  Tabs,
  Text,
  ThemeSwatch,
  Timeline,
  Tree,
  TreeRow,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const noop = (): void => undefined;

const MENU: readonly MenuItem[] = [
  {id: 'window', label: 'Open in new window', press: noop},
  {id: 'archive', label: 'Archive', press: noop},
  {id: 'docs', label: 'Read the guide', href: '#menu'},
  {id: 'delete', label: 'Delete', danger: true, press: noop},
];

function SwatchDemo(): JSX.Element {
  const [light, setLight] = useState<'paper' | 'mist' | 'sand'>('mist');
  return (
    <Grid min="swatch">
      {(['paper', 'mist', 'sand', 'ink', 'midnight', 'terminal'] as const).map(theme => (
        <ThemeSwatch
          key={theme}
          theme={theme}
          label={theme[0]?.toUpperCase() + theme.slice(1)}
          selected={theme === light || theme === 'ink'}
          press={() => {
            if (theme === 'paper' || theme === 'mist' || theme === 'sand') setLight(theme);
          }}
        />
      ))}
    </Grid>
  );
}

function DialogDemo(): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <Row>
      <Button text="Open dialog" press={() => setOpen(true)} />
      <Dialog
        open={open}
        title="Stop at the next safe point?"
        dismiss={() => setOpen(false)}
        actions={
          <>
            <Button variant="quiet" text="Keep going" press={() => setOpen(false)} />
            <Button variant="danger" text="Stop" press={() => setOpen(false)} />
          </>
        }
      >
        <Text tone="muted">
          Work in progress finishes its current step first. Measured results are kept.
        </Text>
      </Dialog>
    </Row>
  );
}

interface Attempt {
  readonly id: string;
  readonly title: string;
  readonly outcome: string;
  readonly delta: string;
  readonly took: string;
}

const ATTEMPTS: readonly Attempt[] = [
  {
    id: 'W1',
    title: 'Pad the head and tail counters',
    outcome: 'accepted',
    delta: '+18.2%',
    took: '6m',
  },
  {id: 'W2', title: 'Batch the producer handoff', outcome: 'rejected', delta: '-3.1%', took: '9m'},
  {
    id: 'W3',
    title: 'Spin before parking a consumer',
    outcome: 'accepted',
    delta: '+7.4%',
    took: '5m',
  },
];

const COLUMNS: readonly TableColumn<Attempt>[] = [
  {key: 'id', header: 'ID', cell: row => row.id, mono: true},
  {key: 'title', header: 'Hypothesis', cell: row => row.title},
  {key: 'outcome', header: 'Outcome', cell: row => row.outcome},
  {key: 'delta', header: 'vs baseline', cell: row => row.delta, mono: true, align: 'end'},
  {key: 'took', header: 'Took', cell: row => row.took, mono: true, align: 'end'},
];

function TableDemo(): JSX.Element {
  const [selected, setSelected] = useState('W2');
  return (
    <Stack>
      <Table
        label="Attempts"
        columns={COLUMNS}
        rows={ATTEMPTS}
        rowKey={row => row.id}
        selected={selected}
        select={setSelected}
      />
      <Table
        label="Attempts, read only"
        columns={COLUMNS}
        rows={ATTEMPTS.slice(0, 2)}
        rowKey={row => row.id}
      />
    </Stack>
  );
}

function TabsDemo(): JSX.Element {
  const [tab, setTab] = useState<'overview' | 'map' | 'work' | 'logs'>('overview');
  return (
    <Tabs
      label="Sections"
      value={tab}
      change={setTab}
      tabs={[
        {value: 'overview', label: 'Overview'},
        {value: 'map', label: 'Map'},
        {value: 'work', label: 'Work'},
        {value: 'logs', label: 'Logs'},
      ]}
    >
      <Text tone="muted">The {tab} panel.</Text>
    </Tabs>
  );
}

function TreeDemo(): JSX.Element {
  const [open, setOpen] = useState<Readonly<Record<string, boolean>>>({g1: true, g2: false});
  const [selected, setSelected] = useState('w2');
  const toggle = (id: string) => (expanded: boolean) => setOpen({...open, [id]: expanded});
  return (
    <Tree label="Workstreams" selected={selected}>
      <TreeRow
        id="g1"
        depth={0}
        title="Contention"
        subtitle="3 items"
        expanded={open['g1'] ?? false}
        toggle={toggle('g1')}
        select={() => setSelected('g1')}
      />
      {open['g1'] === true ? (
        <>
          <TreeRow
            id="w1"
            depth={1}
            title="Pad the counters"
            subtitle="Measured +18.2%"
            state={{tone: 'ok', label: 'Accepted'}}
            chips={[{label: 'accepted', tone: 'ok'}]}
            select={() => setSelected('w1')}
          />
          <TreeRow
            id="w2"
            depth={1}
            title="Spin before parking"
            subtitle="Benchmarking now"
            state={{tone: 'live', label: 'In progress'}}
            chips={[{label: 'builds on W1'}, {label: 'running', tone: 'live'}]}
            select={() => setSelected('w2')}
          />
          <TreeRow
            id="w3"
            depth={1}
            title="Batch the handoff"
            subtitle="Slower than the baseline"
            state={{tone: 'err', label: 'Rejected'}}
            chips={[{label: 'rejected', tone: 'err'}]}
            select={() => setSelected('w3')}
          />
        </>
      ) : null}
      <TreeRow
        id="g2"
        depth={0}
        title="Memory layout"
        subtitle="2 items"
        expanded={open['g2'] ?? false}
        toggle={toggle('g2')}
        select={() => setSelected('g2')}
      />
      {open['g2'] === true ? (
        <TreeRow
          id="w4"
          depth={1}
          title="Align the slots"
          state={{tone: 'neutral', label: 'Waiting'}}
          select={() => setSelected('w4')}
        />
      ) : null}
    </Tree>
  );
}

const CHECKS: readonly CheckItem[] = [
  {id: 'a', state: 'ok', label: 'Settings are valid', detail: 'mpmc, 2 rounds'},
  {
    id: 'b',
    state: 'warn',
    label: 'Image will be built',
    detail: 'The first build takes about 6 minutes',
  },
  {
    id: 'c',
    state: 'fail',
    label: 'Folder is its own repository',
    detail: 'It sits inside ~/src',
    fix: {text: 'Copy it out', press: noop},
  },
  {id: 'd', state: 'pending', label: 'Evaluator is available'},
];

const BUILD_LOG: readonly LogLine[] = [
  {text: '#8 [7/8] WORKDIR /workspace'},
  {text: '#8 DONE 0.0s'},
  {text: '#9 exporting to image'},
  {text: '#9 exporting layers 41.2s'},
];

const STAGES: readonly Stage[] = [
  {id: '1', state: 'done', label: 'Resolving the inputs', elapsed: '1s'},
  {
    id: '2',
    state: 'active',
    label: 'Building the image',
    elapsed: '4m12s',
    quietFor: '2m00s',
    body: <LogPane label="Build output" size="sm" lines={BUILD_LOG} />,
  },
  {
    id: '3',
    state: 'failed',
    label: 'Checking the toolchain',
    elapsed: '3s',
    body: <Text tone="err">The compiler was not found.</Text>,
  },
  {id: '4', state: 'waiting', label: 'Measuring the baseline'},
];

const LOG: readonly LogLine[] = Array.from({length: 40}, (_, index) =>
  index % 9 === 4
    ? {text: `warning: retrying step ${index} after a timeout`, stream: 'err' as const}
    : {text: `[${String(index).padStart(3, '0')}] step ${index} finished in ${(index * 7) % 50}ms`},
);

function ScrubberDemo(): JSX.Element {
  const [at, setAt] = useState(24);
  return (
    <Stack>
      <Scrubber
        label="Position"
        value={at}
        last={40}
        live={at === 40}
        pastLabel="Replay"
        position={`Step ${at} of 40`}
        ticks={[
          {at: 0, label: 'start'},
          {at: 20, label: 'step 20'},
          {at: 40, label: 'now'},
        ]}
        change={setAt}
        backToLive={() => setAt(40)}
      />
      <Scrubber
        label="Position, live"
        value={40}
        last={40}
        live
        pastLabel="Replay"
        position="Step 40 of 40"
        change={noop}
        backToLive={noop}
      />
      <Scrubber
        label="Position, ended"
        value={30}
        last={40}
        live={false}
        pastLabel="Replay"
        latestLabel="End"
        backLabel="Jump to end"
        position="Step 30 of 40"
        change={noop}
        backToLive={noop}
      />
    </Stack>
  );
}

const POINTS = [
  {x: 1, y: 100, filled: true},
  {x: 2, y: 96, filled: false},
  {x: 3, y: 118, filled: true},
  {x: 4, y: 112, filled: false},
  {x: 5, y: 127, filled: true},
  {x: 6, y: 121, filled: false},
];

/** The data display, navigation and overlay components (batch 2 of the design system). */
export const RICH_ENTRIES = {
  AppFrame: {
    render: () => (
      <AppFrame
        label="Sections"
        nav={
          <SidebarSection title="Pages">
            <SidebarItem label="First" icon="plus" href="#first" active />
            <SidebarItem label="Second" icon="folder" href="#second" />
          </SidebarSection>
        }
        footer={<SidebarItem label="Settings" icon="gear" href="#settings" />}
      >
        <Card>
          <Text>The main area holds a Page.</Text>
        </Card>
      </AppFrame>
    ),
  },
  ThemeSwatch: {render: () => <SwatchDemo />},
  Menu: {
    render: () => (
      <Row gap="space-3">
        <Menu icon="more" label="More actions" align="start" items={MENU} />
        <Menu text="Actions" icon="chevron-down" items={MENU} />
      </Row>
    ),
  },
  Dialog: {render: () => <DialogDemo />},
  Table: {render: () => <TableDemo />},
  Tabs: {render: () => <TabsDemo />},
  Tree: {render: () => <TreeDemo />},
  TreeRow: {
    render: () => (
      <Text tone="muted">
        Rows of the Tree above: groups with a disclosure, leaves with a state dot, subtitle and
        chips, one selected.
      </Text>
    ),
  },
  KeyValue: {
    render: () => (
      <KeyValue
        items={[
          {key: 'Waiting on', value: 'The benchmark'},
          {key: 'Attempts', value: '2 of 3', mono: true},
          {key: 'Started', value: '16:42:07', mono: true},
          {key: 'Builds on', value: 'Pad the counters'},
        ]}
      />
    ),
  },
  Prose: {
    render: () => (
      <Card section="Objective">
        <Prose
          blocks={[
            {kind: 'heading', text: 'Make the queue faster'},
            {
              kind: 'paragraph',
              text: 'Maximize trusted end-to-end throughput of the MPMC queue without breaking it.',
            },
            {kind: 'list', ordered: false, items: ['Pass the correctness check', 'Stay lock-free']},
            {kind: 'list', ordered: true, items: ['Measure the baseline', 'Beat it']},
          ]}
        />
      </Card>
    ),
  },
  CheckList: {render: () => <CheckList items={CHECKS} />},
  Timeline: {render: () => <Timeline stages={STAGES} />},
  LogPane: {render: () => <LogPane label="Output" lines={LOG} />},
  Scrubber: {render: () => <ScrubberDemo />},
  Stat: {
    render: () => (
      <Grid min="tile">
        <Card>
          <Stat label="Best" value="+27.0%" sub="over the baseline" />
        </Card>
        <Card>
          <Stat label="Measured" value="14" sub="6 accepted" />
        </Card>
        <Card>
          <Stat label="Elapsed" value="24m" />
        </Card>
      </Grid>
    ),
  },
  Chart: {
    render: () => (
      <Card>
        <Chart
          label="Throughput of each measured attempt relative to the baseline"
          points={POINTS}
          steps={[
            {x: 1, y: 100},
            {x: 3, y: 118},
            {x: 5, y: 127},
            {x: 6, y: 127},
          ]}
          reference={{y: 100, label: 'baseline'}}
          playhead={4.5}
          yTicks={[
            {value: 90, label: '0.9x'},
            {value: 110, label: '1.1x'},
            {value: 130, label: '1.3x'},
          ]}
          xTicks={[1, 2, 3, 4, 5, 6].map(value => ({value, label: String(value)}))}
          xTitle="measurements"
        />
      </Card>
    ),
  },
  SidebarItem: {
    render: () => (
      <Text tone="muted">
        Entries of the SidebarSection below: icons with counts, status dots with details, the active
        one, and a row menu.
      </Text>
    ),
  },
  SidebarSection: {
    render: () => (
      <Grid min="tile">
        <SidebarSection title="Navigate">
          <SidebarItem label="Machines" icon="monitor" count={3} href="#machines" />
          <SidebarItem label="Settings" icon="gear" href="#settings" active />
        </SidebarSection>
        <SidebarSection title="Live">
          <SidebarItem
            label="queue · mpmc"
            detail="step 2 of 2"
            status={{tone: 'live', label: 'In progress'}}
            href="#a"
            active
            menu={MENU}
          />
          <SidebarItem
            label="queue · spsc"
            detail="building 3m"
            status={{tone: 'live', label: 'In progress'}}
            href="#b"
          />
        </SidebarSection>
        <SidebarSection title="Recent" showAll={{label: 'Show all (24)', href: '#all'}}>
          <SidebarItem
            label="prioqueue"
            detail="finished yesterday"
            status={{tone: 'ok', label: 'Finished'}}
            press={noop}
          />
          <SidebarItem
            label="kv-store"
            detail="failed today 16:39"
            status={{tone: 'err', label: 'Failed'}}
            press={noop}
          />
        </SidebarSection>
      </Grid>
    ),
  },
  EmptyState: {
    render: () => (
      <Card>
        <EmptyState
          title="Nothing here yet"
          body="What you start shows up here."
          action={<Button variant="primary" icon="plus" text="Start one" press={noop} />}
        />
      </Card>
    ),
  },
} satisfies Partial<Record<ComponentName, GalleryEntry>>;
