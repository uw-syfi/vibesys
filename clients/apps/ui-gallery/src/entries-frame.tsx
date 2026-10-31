import {
  Breadcrumb,
  Button,
  Card,
  Dock,
  DotLegend,
  Fact,
  FactLine,
  Gantt,
  type GanttRow,
  type GanttView,
  type Guide,
  LoadingBar,
  LogPane,
  Meter,
  MetricTile,
  MetricTiles,
  Narrative,
  NewBelow,
  Pane,
  Panes,
  PickList,
  PickRow,
  Reserve,
  Row,
  Said,
  Screen,
  SearchBox,
  Sparkline,
  Stack,
  StackedArea,
  StackedBars,
  Swatch,
  SwatchLegend,
  Text,
  Thought,
  ToolGroup,
  type ToolLine,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';
import {BUILDER_LOWER, PLANNER_LOWER, REVIEWER_LOWER} from './role-words.js';

const SPARK = [
  {x: 0, y: 1.58},
  {x: 3, y: 2.4},
  {x: 6, y: 3.1},
  {x: 8, y: 6.05},
];

const noop = (): void => undefined;

const USAGE_BARS = [
  [4, 6, 0],
  [8, 12, 3],
  [2, 20, 5],
  [0, 9, 2],
  [6, 0, 0],
].map((parts, index) => ({
  id: `bar-${index}`,
  title: `Bar ${index + 1}: ${parts.reduce((sum, part) => sum + part, 0)}k tokens`,
  total: parts.reduce((sum, part) => sum + part, 0),
  segments: parts.flatMap((value, at) =>
    value === 0 ? [] : [{key: `${index}-${at}`, series: [7, 1, 3][at] ?? 0, value}],
  ),
}));

interface Candidate {
  readonly id: string;
  readonly guides: readonly Guide[];
  readonly idea: string;
  readonly share: number;
  readonly figure: string;
  readonly state: 'passed' | 'failed' | 'running';
}

const CANDIDATES: readonly Candidate[] = [
  {id: 'input', guides: [], idea: 'your project', share: 0.17, figure: '1.00×', state: 'passed'},
  {
    id: 'ring-v1',
    guides: ['elbow'],
    idea: 'sequence ring',
    share: 0.7,
    figure: '4.09×',
    state: 'passed',
  },
  {
    id: 'padding',
    guides: ['blank', 'tee'],
    idea: 'pad counters',
    share: 1,
    figure: '5.72×',
    state: 'passed',
  },
  {
    id: 'batching',
    guides: ['blank', 'elbow'],
    idea: 'batch dequeue',
    share: 0.6,
    figure: '3.82×',
    state: 'failed',
  },
  {id: 'backoff', guides: [], idea: 'spin backoff', share: 0, figure: '1m 05s', state: 'running'},
];

const STATE_DOT = {
  passed: {tone: 'ok', label: 'passed'},
  failed: {tone: 'err', label: 'failed · turned down'},
  running: {tone: 'live', label: 'running'},
} as const;

function CandidateList(props: {
  readonly selected: string | undefined;
  readonly pick: (id: string) => void;
}): JSX.Element {
  const [pointed, setPointed] = useState<string | null>(null);
  return (
    <PickList
      label="Candidates"
      header={{
        start: 'candidate · built on · idea',
        end: [{label: 'vs best', bar: true}, {label: 'vs input'}],
      }}
      {...(props.selected === undefined ? {} : {selected: props.selected})}
    >
      {CANDIDATES.map(candidate => (
        <PickRow
          key={candidate.id}
          id={candidate.id}
          guides={candidate.guides}
          title={candidate.id}
          mono
          summary={candidate.idea}
          state={STATE_DOT[candidate.state]}
          {...(candidate.id === 'padding' ? {badges: [{label: 'best', tone: 'ok' as const}]} : {})}
          {...(candidate.id === 'batching'
            ? {badges: [{label: '×3', tone: 'neutral' as const}]}
            : {})}
          {...(candidate.state === 'running'
            ? {}
            : {
                bar: {
                  value: candidate.share,
                  tone: 'ok' as const,
                  label: `${candidate.share * 100}% of the best`,
                },
              })}
          figure={candidate.figure}
          figureTone={
            candidate.state === 'failed' ? 'err' : candidate.state === 'running' ? 'muted' : 'ok'
          }
          path={pointed === 'batching' && ['input', 'ring-v1', 'batching'].includes(candidate.id)}
          hover={on => setPointed(on ? candidate.id : null)}
          fresh={candidate.state === 'running'}
          select={() => props.pick(candidate.id)}
        />
      ))}
    </PickList>
  );
}

function PickListDemo(): JSX.Element {
  const [selected, setSelected] = useState<string | undefined>('ring-v1');
  return (
    <Card>
      <CandidateList selected={selected} pick={setSelected} />
    </Card>
  );
}

const ROWS: readonly GanttRow[] = [
  {
    id: 'input',
    label: 'input',
    start: 0,
    end: 1,
    outcome: 'muted',
    press: noop,
    figure: '1.00×',
    segments: [
      {
        id: 'gate',
        start: 0,
        end: 1,
        kind: 'hatched',
        label: 'gate · 2.33M ops/s · 41s',
        press: noop,
      },
    ],
  },
  {
    id: 'ring-v1',
    label: 'ring-v1',
    guides: ['elbow'],
    start: 1,
    end: 7,
    outcome: 'ok',
    press: noop,
    figure: '4.09×',
    selected: true,
    segments: [
      {
        id: 'plan',
        start: 1,
        end: 2,
        kind: 'grey',
        label: `${REVIEWER_LOWER} · picked the idea · 1m`,
        press: noop,
      },
      {
        id: 'impl',
        start: 2,
        end: 5,
        kind: 'solid',
        label: `${BUILDER_LOWER} · wrote the ring · 3m`,
        press: noop,
      },
      {
        id: 'gate',
        start: 5,
        end: 7,
        kind: 'hatched',
        label: 'gate · 9.53M ops/s · 2m',
        press: noop,
      },
    ],
  },
  {
    id: 'batching',
    label: 'batching',
    guides: ['blank', 'elbow'],
    start: 7,
    end: 11,
    outcome: 'err',
    press: noop,
    figure: '3.82×',
    segments: [
      {
        id: 'impl',
        start: 7,
        end: 9,
        kind: 'solid',
        label: `${BUILDER_LOWER} · batched dequeues · 2m`,
        press: noop,
      },
      {
        id: 'gate',
        start: 9,
        end: 11,
        kind: 'hatched',
        label: 'gate · slower than parent · 2m',
        press: noop,
      },
    ],
  },
  {
    id: 'backoff',
    label: 'backoff',
    start: 8,
    end: 14,
    outcome: 'live',
    press: noop,
    fresh: true,
    segments: [
      {
        id: 'impl',
        start: 8,
        end: 14,
        kind: 'solid',
        label: `${BUILDER_LOWER} · editing · 6m`,
        press: noop,
      },
    ],
  },
];

const TICKS = [0, 5, 10].map(at => ({at, label: `${at}m`}));

const SERIES_ROWS: readonly GanttRow[] = [
  {
    id: 'ring-v1',
    label: 'ring-v1',
    start: 0,
    end: 9,
    outcome: 'ok',
    press: noop,
    figure: '1.40×',
    segments: [
      {id: 'a', start: 0, end: 3, kind: 'solid', series: 1, label: `${BUILDER_LOWER} · 3m`},
      {id: 'b', start: 3, end: 6, kind: 'hatched', label: 'gate · 3m'},
      {id: 'c', start: 6, end: 9, kind: 'solid', series: 3, label: `${REVIEWER_LOWER} · 3m`},
    ],
  },
  {
    id: 'ring-v2',
    label: 'ring-v2',
    start: 4,
    end: 12,
    outcome: 'err',
    press: noop,
    figure: 'failed',
    segments: [
      {id: 'd', start: 4, end: 8, kind: 'solid', series: 1, label: `${BUILDER_LOWER} · 4m`},
      {id: 'e', start: 8, end: 12, kind: 'hatched', label: 'gate · 4m'},
    ],
  },
];

const STEP_STRIP: GanttRow = {
  id: 'steps',
  label: 'ring-v1 steps over time',
  start: 0,
  end: 9,
  outcome: 'ok',
  press: noop,
  figure: '1.40×',
  segments: [
    {id: 'a', start: 0, end: 3, kind: 'solid', series: 1, label: `${BUILDER_LOWER} · 3m`},
    {id: 'b', start: 3, end: 6, kind: 'hatched', label: 'gate · 3m'},
    {id: 'c', start: 6, end: 9, kind: 'solid', series: 3, label: `${REVIEWER_LOWER} · 3m`},
  ],
};

/** Bars colored per role, with the legend that names each color; and a strip of steps with its rows. */
function SeriesBarsDemo(): JSX.Element {
  const key = [
    {label: BUILDER_LOWER, series: 1},
    {label: REVIEWER_LOWER, series: 3},
    {label: 'gate', hatched: true},
  ];
  return (
    <Card>
      <Stack>
        <Stack gap="space-1">
          <SwatchLegend label="Step colors" items={key} />
          <Gantt label="Steps over time" strip from={0} to={9} rows={[STEP_STRIP]} />
        </Stack>
        <PickList label="Steps">
          <PickRow id="a" title={BUILDER_LOWER} swatch={1} figure="3m" select={noop} />
          <PickRow id="b" title="gate" hatched figure="3m" select={noop} />
          <PickRow
            id="c"
            title={REVIEWER_LOWER}
            swatch={3}
            badges={[{label: 'failed', tone: 'err'}]}
            figure="3m"
            select={noop}
          />
        </PickList>
        <SwatchLegend
          label="Bar colors"
          items={[
            {label: BUILDER_LOWER, series: 1},
            {label: REVIEWER_LOWER, series: 3},
            {label: 'gate', hatched: true},
          ]}
        />
        <Gantt label="Roles over time" from={0} to={12} ticks={TICKS} rows={SERIES_ROWS} />
      </Stack>
    </Card>
  );
}

function GanttDemo(): JSX.Element {
  const [view, setView] = useState<GanttView>({from: 0, to: 14});
  return (
    <Stack>
      <Card>
        <Gantt
          label="Candidates over time"
          nameLabel="candidate · built on"
          figureLabel="vs input"
          from={0}
          to={14}
          playhead={14}
          ticks={TICKS}
          view={view}
          changeView={setView}
          rows={ROWS}
        />
      </Card>
      <Card>
        <Gantt
          label="ring-v1 steps over time"
          strip
          from={1}
          to={7}
          rows={ROWS.slice(1, 2).map(row => ({...row, selected: false}))}
        />
      </Card>
    </Stack>
  );
}

function FilterDemo(): JSX.Element {
  const [failed, setFailed] = useState(false);
  const [text, setText] = useState('');
  return (
    <Row gap="space-4" align="center">
      <DotLegend
        items={[
          {tone: 'ok', label: '40 passed', on: true, toggle: noop},
          {tone: 'live', label: '3 running', on: true, toggle: noop},
          {tone: 'err', label: '557 failed', on: failed, toggle: setFailed},
        ]}
      />
      <SearchBox label="Search ideas" value={text} change={setText} placeholder="Search ideas" />
    </Row>
  );
}

function ScreenDemo(): JSX.Element {
  const [selected, setSelected] = useState<string | undefined>('padding');
  return (
    <Screen
      density="compact"
      landmark="none"
      header={
        <Row justify="between" align="center">
          <Text as="h3" variant="heading">
            queue-rs · mpmc
          </Text>
          <Button size="sm" variant="secondary" text="Pause" press={noop} />
        </Row>
      }
    >
      <Breadcrumb
        label="Where in the screen"
        items={[
          {id: 'run', label: 'Run', press: noop},
          {id: 'candidate', label: 'padding'},
        ]}
      />
      <Panes>
        <Pane
          kicker="candidates · idea + code"
          title="5"
          width="list"
          meta={
            <DotLegend
              items={[
                {tone: 'ok', label: '3 passed'},
                {tone: 'err', label: '1 failed'},
                {tone: 'live', label: '1 running'},
              ]}
            />
          }
        >
          <CandidateList selected={selected} pick={setSelected} />
        </Pane>
        <Pane
          kicker="candidate"
          title="Pad the head and tail counters"
          width="fill"
          enterKey="padding"
        >
          <LogPane
            size="fill"
            label="Gate log"
            lines={[
              {text: '$ cargo test --locked'},
              {text: 'test result: ok. 18 passed; 0 failed'},
              {text: 'warning: cpu frequency scaling is enabled', stream: 'err'},
            ]}
          />
        </Pane>
      </Panes>
    </Screen>
  );
}

const TOOLS: readonly ToolLine[] = [
  {id: 't1', name: 'read', line: 'src/lib.rs', outcome: '212 lines', state: 'ok', press: noop},
  {
    id: 't2',
    name: 'execute',
    line: 'cargo test --locked',
    outcome: 'exit 101',
    state: 'failed',
    press: noop,
  },
  {
    id: 't3',
    name: 'edit',
    line: 'src/lib.rs',
    outcome: '+38 -12',
    state: 'ok',
    selected: true,
    press: noop,
  },
];

function NarrativeDemo(): JSX.Element {
  const [opened, setOpened] = useState(true);
  return (
    <Narrative label="Turn">
      <Thought text="Throughput flattens past four producers; suspect false sharing on the counters." />
      <ToolGroup tools={TOOLS.slice(0, 1)} open={false} toggle={noop} />
      <Said text="Head and tail share a cache line. Padding each to 128 bytes should stop it." />
      <ToolGroup tools={TOOLS} open={opened} toggle={setOpened} />
      <ToolGroup tools={TOOLS.slice(1, 2)} open={false} toggle={noop} />
    </Narrative>
  );
}

/** The frame: a screen that never scrolls as a whole, its panes and lists, and a time axis. */
export const FRAME_ENTRIES = {
  Screen: {render: () => <ScreenDemo />},
  Breadcrumb: {
    render: () => (
      <Breadcrumb
        label="Zoom"
        items={[
          {id: 'run', label: 'Run', press: noop},
          {id: 'candidate', label: 'padding', press: noop},
          {id: 'step', label: BUILDER_LOWER, press: noop},
          {id: 'item', label: 'cargo test --locked'},
        ]}
      />
    ),
  },
  Panes: {
    render: () => (
      <Text tone="muted">
        The row of panes in the Screen above: a list pane beside the pane its selection opened.
      </Text>
    ),
  },
  Pane: {
    render: () => (
      <Text tone="muted">
        The panes of the Screen above: a kicker, a title and facts on one line, a body that scrolls
        by itself, and a slide-in with a flash on the header when a selection opens it.
      </Text>
    ),
  },
  PickList: {render: () => <PickListDemo />},
  PickRow: {
    render: () => (
      <Text tone="muted">
        Rows of the PickList above: one line with lineage guides, a state dot that names its state
        under the pointer and the focus, badges, a share bar and a figure; the path to the root lit
        while a row is pointed at; one row selected, one fresh.
      </Text>
    ),
  },
  Meter: {
    render: () => (
      <Stack>
        <Meter label="Accent, 30% of the best" value={0.3} />
        <Meter label="Ok, 70% of the best" value={0.7} tone="ok" />
        <Meter label="Muted, all of it" value={1} tone="muted" />
      </Stack>
    ),
  },
  LoadingBar: {
    render: () => <LoadingBar label="Loading, total unknown" />,
  },
  MetricTiles: {
    render: () => (
      <Stack>
        <MetricTiles label="Performance" count={2}>
          <MetricTile
            name="Throughput"
            figure="3.83×"
            note="faster than input"
            tone="ok"
            detail="6,054,154 ops/s · input 1,581,539 ops/s"
            trend={
              <Sparkline
                label="Throughput, best so far"
                steps={SPARK}
                from={0}
                to={10}
                reference={1.58}
              />
            }
          />
          <MetricTile
            name="p99 latency"
            figure="0.80×"
            note="slower than input"
            tone="err"
            detail="12.4 ms · input 9.9 ms"
          />
        </MetricTiles>
        <MetricTiles label="Performance, stacked" count={2} stacked>
          <MetricTile
            name="Throughput"
            figure="3.83×"
            note="faster than input"
            tone="ok"
            detail="6,054,154 ops/s · input 1,581,539 ops/s"
          />
          <MetricTile
            name="p99 latency"
            figure="1.43×"
            note="faster than input"
            tone="ok"
            detail="8.7 ms · input 12.4 ms"
          />
        </MetricTiles>
      </Stack>
    ),
  },
  MetricTile: {
    render: () => (
      <MetricTile
        name="Throughput"
        figure="1.18×"
        note="faster than input"
        tone="ok"
        detail="48.6 ops/s · input 41.2 ops/s"
      />
    ),
  },
  Sparkline: {
    render: () => <Sparkline label="Best so far" steps={SPARK} from={0} to={10} reference={1.58} />,
  },
  NewBelow: {render: () => <NewBelow text="3 new" press={noop} />},
  Gantt: {render: () => <GanttDemo />},
  Narrative: {render: () => <NarrativeDemo />},
  Thought: {
    render: () => (
      <Text tone="muted">The muted, italic paragraphs of the Narrative above: the thinking.</Text>
    ),
  },
  Said: {
    render: () => (
      <Text tone="muted">The body paragraphs of the Narrative above: what was said.</Text>
    ),
  },
  ToolGroup: {
    render: () => (
      <Text tone="muted">
        The dim lines of the Narrative above: tool calls folded into one line that opens in place; a
        failed call stays visible when closed, and the selected call is marked.
      </Text>
    ),
  },
  FactLine: {
    render: () => (
      <Stack>
        <FactLine>
          <Fact>
            <Text variant="code">Passed</Text>
          </Fact>
          <Fact>
            <Text variant="code" tone="muted">
              0.91× vs base
            </Text>
          </Fact>
          <Fact
            shrink
            title="Replace the per-slot sequence numbers of the lock-free ring with a contiguous payload arena"
          >
            <Text variant="small" tone="muted">
              from
            </Text>
            <Text variant="small">
              Replace the per-slot sequence numbers of the lock-free ring with a contiguous payload
              arena and batched cursor publication
            </Text>
          </Fact>
        </FactLine>
        <FactLine wrap={false}>
          <Fact>
            <Text variant="code">3.83× vs input</Text>
          </Fact>
          <Fact shrink>
            <Text variant="code" tone="muted">
              this idea · 6,054,154 total_ops_per_sec with a name too long for the line
            </Text>
          </Fact>
        </FactLine>
      </Stack>
    ),
  },
  Fact: {
    render: () => (
      <Text tone="muted">Facts are the entries of the FactLine above: whole, or shrinking.</Text>
    ),
  },
  Dock: {
    render: () => (
      <Dock
        label="Chat"
        side={
          <Stack gap="space-3">
            <Text variant="section">Chat</Text>
            <Text tone="muted">Docked full height.</Text>
          </Stack>
        }
      >
        <Text>The screen keeps its content and narrows.</Text>
      </Dock>
    ),
  },
  Reserve: {
    render: () => (
      <Stack gap="space-1">
        <Reserve>
          <Text variant="code">Held room for a line that is not there yet</Text>
        </Reserve>
        <Text variant="code">The figure under it does not move when the line arrives.</Text>
      </Stack>
    ),
  },
  DotLegend: {render: () => <FilterDemo />},
  SwatchLegend: {render: () => <SeriesBarsDemo />},
  Swatch: {
    render: () => (
      <Text tone="muted">
        The square before a name in a legend or a table row: a color of the palette, or hatched for
        what has none.
      </Text>
    ),
  },
  StackedArea: {
    render: () => (
      <StackedArea
        label="Tokens, running total"
        unit="tokens"
        from="Oct 15, 16:30"
        to="Oct 15, 19:10"
        titles={['16:30 to 17:00', '17:00 to 17:30', '17:30 to 18:00', '18:00 to 18:30']}
        layers={[
          {key: PLANNER_LOWER, series: 7, values: [4, 8, 10, 12]},
          {key: BUILDER_LOWER, series: 1, values: [6, 18, 38, 47]},
          {key: REVIEWER_LOWER, series: 3, values: [0, 3, 8, 13]},
        ]}
      />
    ),
  },
  StackedBars: {
    render: () => (
      <Stack>
        <StackedBars
          label="Tokens over time"
          unit="tokens"
          from="Oct 15, 16:30"
          to="Oct 15, 19:10"
          bars={USAGE_BARS}
        />
        <SwatchLegend
          label="Roles"
          items={[
            {label: PLANNER_LOWER, series: 7},
            {label: BUILDER_LOWER, series: 1},
            {label: REVIEWER_LOWER, series: 3},
          ]}
        />
        <Row gap="space-2" align="center">
          <Swatch series={1} />
          <Text variant="small">{BUILDER_LOWER}</Text>
        </Row>
      </Stack>
    ),
  },
  SearchBox: {
    render: () => <Text tone="muted">The search field beside the legend filter chips above.</Text>,
  },
} satisfies Partial<Record<ComponentName, GalleryEntry>>;
