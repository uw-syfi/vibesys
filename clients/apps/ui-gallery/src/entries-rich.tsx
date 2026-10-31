import {
  AppFrame,
  Button,
  Card,
  Chart,
  ChartColumns,
  ChartLegend,
  ChartPanel,
  type ChartPoint,
  type CheckItem,
  CheckList,
  Dialog,
  EmptyState,
  Grid,
  KeyValue,
  type LogLine,
  LogPane,
  Markdown,
  Menu,
  MenuBounds,
  type MenuItem,
  type ParetoAxis,
  ParetoChart,
  type ParetoPoint,
  Prose,
  Row,
  Scrubber,
  SidebarGroup,
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
  WindowFrame,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const noop = (): void => undefined;

const MARKDOWN_SAMPLE = `## Plan

Pad the **head** and **tail** counters so each sits on its own cache line, then re-run the \`mpsc\` bench.

- Expected: throughput *flat* past four producers, not ~~falling~~
- Risk: the padding grows \`Queue\` by 128 bytes
  - mitigated by sharing one allocation

1. Measure the baseline
2. Apply the padding
3. Compare against [the notes](https://example.com/notes)

| Producers | Before | After |
| --- | --- | --- |
| 1 | 31.2 | 31.0 |
| 8 | 9.4 | 27.8 |

\`\`\`rust
#[repr(align(128))]
struct Padded<T>(T); // a long line in a code block scrolls inside the block, never the page
\`\`\`

> Raw HTML such as <b>this</b> is shown as text, and an image is not loaded: ![the chart](https://example.com/chart.png)`;

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
          Work in progress finishes its current step first. Measured results stay.
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
    outcome: 'passed',
    delta: '+18.2%',
    took: '6m',
  },
  {id: 'W2', title: 'Batch the producer handoff', outcome: 'failed', delta: '-3.1%', took: '9m'},
  {
    id: 'W3',
    title: 'Spin before parking a consumer',
    outcome: 'passed',
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
            state={{tone: 'ok', label: 'Passed'}}
            chips={[{label: 'passed', tone: 'ok'}]}
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
            state={{tone: 'err', label: 'Failed'}}
            chips={[{label: 'failed', tone: 'err'}]}
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

const PICK_POINTS: readonly ChartPoint[] = [
  {id: 'ring', label: 'ring · 118 · 1.18×', x: 3, y: 118},
  {id: 'batching', label: 'batching · 109 · 1.09×', x: 5, y: 109},
  {id: 'padding', label: 'padding · 127 · 1.27×', x: 6, y: 127},
];

function PickChartDemo(): JSX.Element {
  const [selected, setSelected] = useState('ring');
  return (
    <Chart
      label="Throughput of each reading; pick a dot to select it"
      size="strip"
      points={PICK_POINTS.map(point => ({...point, selected: point.id === selected}))}
      steps={[
        {x: 0, y: 100},
        {x: 3, y: 118},
        {x: 6, y: 127},
        {x: 7, y: 127},
      ]}
      start="Input"
      pick={setSelected}
      yTicks={[100, 120].map(value => ({value, label: `${value / 100}x`}))}
      xTicks={[0, 3, 6].map(value => ({value, label: `${value}m`}))}
      xDomain={[0, 7]}
    />
  );
}

const LARGE_POINTS = [
  {x: 1, y: 18_000_000},
  {x: 2, y: 17_200_000},
  {x: 3, y: 21_500_000},
  {x: 4, y: 20_100_000},
  {x: 5, y: 24_000_000},
  {x: 6, y: 22_800_000},
];

const PARETO_THROUGHPUT: ParetoAxis = {name: 'Throughput', unit: 'ops/s', direction: 'higher'};
const PARETO_LATENCY: ParetoAxis = {name: 'Latency', unit: 'ms', direction: 'lower'};

/** A run of twenty ideas that trade latency for throughput, measured an hour apart; two turned down. */
const PARETO_POINTS: readonly ParetoPoint[] = (() => {
  let seed = 5;
  const next = (): number => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const measured = Array.from({length: 20}, (_, index): ParetoPoint => {
    const throughput = 18_000_000 + index * 450_000 + (next() - 0.4) * 2_600_000;
    const latency = 52 - index * 0.7 + (next() - 0.5) * 16 + (throughput - 20_000_000) / 400_000;
    return {
      id: `p${index + 1}`,
      label: `Idea ${index + 1}`,
      x: Math.round(throughput),
      y: Math.round(latency * 10) / 10,
      time: index + 1,
      state: index === 6 || index === 13 ? 'failed' : 'passed',
    };
  });
  return [
    {id: 'input', label: 'Input', x: 18_000_000, y: 52, time: 0, state: 'input'},
    ...measured,
  ];
})();

const POINTS = [
  {x: 1, y: 100},
  {x: 2, y: 96},
  {x: 3, y: 118},
  {x: 4, y: 112},
  {x: 5, y: 127},
  {x: 6, y: 121},
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
  WindowFrame: {
    render: () => (
      <WindowFrame>
        <Card>
          <Text>A window that is only a page, clear of the window's own controls.</Text>
        </Card>
      </WindowFrame>
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
  MenuBounds: {
    render: () => (
      <MenuBounds>
        <Row justify="between" gap="space-3">
          <Text>Opens under this row</Text>
          <Menu text="Bounded" icon="chevron-down" items={MENU} />
        </Row>
      </MenuBounds>
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
          <Stat label="Measured" value="14" sub="6 passed" />
        </Card>
        <Card>
          <Stat label="Elapsed" value="24m" />
        </Card>
      </Grid>
    ),
  },
  Chart: {
    render: () => (
      <Stack>
        <Card>
          <Chart
            label="Throughput of each reading, with the best so far, scrubbed to the middle"
            points={POINTS.map(point => ({...point, label: `reading at ${point.x}`}))}
            steps={[
              {x: 0, y: 100},
              {x: 3, y: 118},
              {x: 5, y: 127},
              {x: 6, y: 127},
            ]}
            start="Input"
            cursor={3.5}
            marks={[2, 4]}
            markLabel="resumed"
            caption="running time · 6m"
            yTicks={[
              {value: 90, label: '0.9x'},
              {value: 110, label: '1.1x'},
              {value: 130, label: '1.3x'},
            ]}
            xTicks={[0, 1, 2, 3, 4, 5, 6].map(value => ({value, label: `${value}m`}))}
            xDomain={[0, 6]}
          />
        </Card>
        <Card>
          <Chart
            label="Throughput in operations per second, with large values and a baseline"
            points={LARGE_POINTS}
            steps={[
              {x: 0, y: 18_000_000},
              {x: 3, y: 21_500_000},
              {x: 5, y: 24_000_000},
              {x: 6, y: 24_000_000},
            ]}
            start="Input"
            references={[{y: 20_000_000, label: 'baseline'}]}
            yTicks={[16_000_000, 20_000_000, 24_000_000].map(value => ({
              value,
              label: value.toLocaleString('en-US'),
            }))}
            xTicks={[0, 1, 2, 3, 4, 5, 6].map(value => ({value, label: `${value}m`}))}
            xDomain={[0, 6]}
          />
        </Card>
        <Card>
          <PickChartDemo />
        </Card>
        <Card>
          <Chart
            label="Throughput with one reading ringed and the steps of its lineage ringed in the accent"
            size="strip"
            points={[
              {id: 'a', x: 1, y: 100, lit: true},
              {id: 'b', x: 2, y: 90},
              {id: 'c', x: 3, y: 130, selected: true, lit: true},
              {id: 'd', x: 4, y: 40},
              {id: 'e', x: 5, y: 120},
            ]}
            steps={[
              {x: 0, y: 90},
              {x: 1, y: 100},
              {x: 3, y: 130},
              {x: 6, y: 130},
            ]}
            yDomain={[0, 143]}
            yTicks={[0, 50, 100].map(value => ({value, label: String(value)}))}
            xTicks={[0, 2, 4, 6].map(value => ({value, label: `${value}m`}))}
            xDomain={[0, 6]}
            pick={noop}
          />
        </Card>
      </Stack>
    ),
  },
  ChartLegend: {
    render: () => (
      <Stack>
        <ChartLegend
          label="What the chart draws"
          items={[
            {label: 'Best so far', mark: 'line'},
            {label: 'Reading', mark: 'dot'},
          ]}
        />
        <ChartLegend
          label="What the chart draws, with an idea selected"
          items={[
            {label: 'Best so far', mark: 'line'},
            {label: 'Reading', mark: 'dot'},
            {label: 'Selected', mark: 'ring'},
          ]}
        />
      </Stack>
    ),
  },
  ChartPanel: {
    render: () => (
      <Stack>
        <ChartPanel
          title="Throughput"
          note="higher is better"
          figure="3.83× faster"
          tone="ok"
          hint="6,054,154 ops/s · input 1,581,539 ops/s"
          unit="ops/s"
        >
          <Chart
            label="Throughput"
            size="strip"
            points={PICK_POINTS}
            steps={[
              {x: 0, y: 100},
              {x: 6, y: 127},
            ]}
            start="Input"
            xTicks={[0, 3, 6].map(value => ({value, label: `${value}m`}))}
            xDomain={[0, 6]}
          />
        </ChartPanel>
        <ChartPanel title="Queue depth" note="lower is better" figure="No clear change">
          <Chart label="Queue depth" size="strip" points={[]} />
        </ChartPanel>
      </Stack>
    ),
  },
  ChartColumns: {
    render: () => (
      <ChartColumns>
        {['Time to first token', 'Time per output token', 'Completed requests'].map(title => (
          <ChartPanel
            key={title}
            title={title}
            note="lower is better"
            figure="1.78× faster"
            tone="ok"
          >
            <Chart
              label={title}
              size="strip"
              points={PICK_POINTS}
              steps={[
                {x: 0, y: 100},
                {x: 6, y: 127},
              ]}
              start="Input"
              xTicks={[0, 3, 6].map(value => ({value, label: `${value}m`}))}
              xDomain={[0, 6]}
            />
          </ChartPanel>
        ))}
      </ChartColumns>
    ),
  },
  ParetoChart: {
    render: () => (
      <Stack>
        <Card>
          <ParetoChart
            label="Throughput and latency of each measured idea, with the Pareto frontier"
            points={PARETO_POINTS}
            x={PARETO_THROUGHPUT}
            y={PARETO_LATENCY}
            highlighted={['p9']}
            pick={noop}
          />
        </Card>
        <Grid min="tile">
          <Card>
            <ParetoChart
              label="The same run replayed to hour 8: fewer points, an earlier frontier"
              size="strip"
              points={PARETO_POINTS}
              x={PARETO_THROUGHPUT}
              y={PARETO_LATENCY}
              cursor={8}
            />
          </Card>
          <Card>
            <ParetoChart
              label="Speed-up and memory saved, both higher is better, in one idea's lineage"
              size="strip"
              points={[
                {id: 'in', label: 'Input', x: 1, y: 0, time: 0, state: 'input'},
                {id: 'a', label: 'Arena', x: 1.2, y: 30, time: 1, state: 'passed'},
                {id: 'b', label: 'Pooling', x: 1.5, y: 12, time: 2, state: 'passed'},
                {id: 'c', label: 'Packing', x: 1.1, y: 55, time: 3, state: 'passed'},
                {id: 'd', label: 'Both', x: 1.6, y: 40, time: 4, state: 'passed'},
              ]}
              x={{name: 'Speed-up', unit: '×', direction: 'higher'}}
              y={{name: 'Memory saved', unit: '%', direction: 'higher'}}
            />
          </Card>
        </Grid>
      </Stack>
    ),
  },
  SidebarItem: {
    render: () => (
      <Stack gap="space-4">
        <Text tone="muted">
          Entries of the SidebarSection below: icons with counts, status dots with details, the
          active one, and a row menu. Numbered markers are the steps of a form.
        </Text>
        <Grid min="tile">
          <SidebarSection title="Steps">
            <SidebarItem
              label="Start from"
              detail="An example"
              marker={{text: '1', state: 'neutral', label: 'Step 1, answered'}}
              press={noop}
            />
            <SidebarItem
              label="Hardware"
              active
              marker={{text: '2', state: 'current', label: 'Step 2, not answered yet'}}
              press={noop}
            />
            <SidebarItem
              label="Review and start"
              marker={{text: '3', state: 'muted', label: 'Step 3, not answered yet'}}
              disabled
              press={noop}
            />
          </SidebarSection>
        </Grid>
      </Stack>
    ),
  },
  SidebarGroup: {
    render: () => (
      <Grid min="tile">
        <SidebarSection title="Runs by task">
          <SidebarGroup title="queue-rs · mpmc" note="2 runs">
            <SidebarItem
              compact
              label="07:32 · 3.83×"
              trail="10 ideas · 52m"
              hint="queue-rs · mpmc"
              status={{tone: 'ok', label: 'finished'}}
              press={noop}
            />
            <SidebarItem
              compact
              label="Yesterday 21:10 · stopped · 2 ideas"
              trail="28m"
              hint="queue-rs · mpmc"
              status={{tone: 'neutral', label: 'stopped'}}
              press={noop}
            />
          </SidebarGroup>
          <SidebarGroup title="kv-store · get-put">
            <SidebarItem
              compact
              label="can't read run state"
              status={{tone: 'neutral', label: "can't read run state"}}
              action={{label: 'Retry', press: noop}}
              press={noop}
            />
          </SidebarGroup>
        </SidebarSection>
      </Grid>
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
  Markdown: {
    render: () => (
      <Card>
        <Markdown text={MARKDOWN_SAMPLE} />
      </Card>
    ),
  },
  LinkOpener: {
    render: () => (
      <Text tone="muted">
        Wraps the Markdown above to say where its links open: a web or mail address is handed to the
        app's opener and the page never navigates.
      </Text>
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
