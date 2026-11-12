import {
  Card,
  Chip,
  type Crumb,
  Gantt,
  type GanttLegend,
  type GanttRow,
  type Guide,
  Page,
  Text,
} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import {
  type AgentRow,
  agentNames,
  type Branch,
  type FoldChoices,
  foldedCount,
  type GroupRow,
  isFolded,
  type OrchestratorState,
  type SpawnRow,
  type SpawnTree,
  spawnTree,
  toggled,
} from '../../state/index.js';
import {useDisplayClock} from '../display-clock.js';
import {agentLabel, groupLabel, runtimeLabel} from './labels.js';
import {formatSpan, formatUsd} from './time.js';

/** What the bars of the tree say, in the words the page's own chips and column use. */
const LEGEND: GanttLegend = {
  live: 'Running',
  done: 'Done',
  failed: 'Failed',
  returned: 'Result returned',
  tag: 'Runtime',
  folded: 'Folded',
};

/** The lineage cells before a row's name: a line through each ancestor that has later siblings, a branch for the row. */
export function guidesOf(lineage: readonly Branch[]): Guide[] {
  return lineage.map((branch, level): Guide => {
    if (level < lineage.length - 1) return branch === 'more' ? 'pipe' : 'blank';
    return branch === 'more' ? 'tee' : 'elbow';
  });
}

const KIND = {running: 'solid', done: 'grey', failed: 'err'} as const;

interface Actions {
  readonly open: (agentId: string) => void;
  readonly fold: (row: SpawnRow) => void;
}

/** What every row has: its place, its span and its bar. */
function shared(row: SpawnRow, label: string): GanttRow {
  return {
    id: row.key,
    label,
    guides: guidesOf(row.lineage),
    start: row.startMs,
    end: row.endMs,
    outcome: row.state === 'failed' ? 'err' : 'muted',
    segments: [
      {
        id: row.key,
        start: row.startMs,
        end: row.endMs,
        kind: KIND[row.state],
        label: `${label}: ${formatSpan(row.endMs - row.startMs)}`,
      },
    ],
    press: () => undefined,
  };
}

function agentRow(row: AgentRow, labels: ReadonlyMap<string, string>, actions: Actions): GanttRow {
  const fold = () => actions.fold(row);
  const count = foldedCount(row);
  return {
    ...shared(row, labels.get(row.agentId) ?? ''),
    tag: runtimeLabel(row.runtime),
    ...(row.costUsd === null ? {} : {figure: formatUsd(row.costUsd)}),
    ...(row.children > 0 ? {fold: {open: !isFolded(row), toggle: fold}} : {}),
    ...(count > 0 ? {count} : {}),
    marks: row.ticks.map(tick => ({
      at: tick.atMs,
      tone: tick.tone === 'failed' ? 'err' : 'ok',
      label: labels.get(tick.childId) ?? '',
    })),
    press: () => actions.open(row.agentId),
  };
}

function groupRow(row: GroupRow, actions: Actions): GanttRow {
  const fold = () => actions.fold(row);
  const [only] = row.runtimes;
  return {
    ...shared(row, groupLabel(row.role)),
    tag: only !== undefined && row.runtimes.length === 1 ? runtimeLabel(only) : 'Mixed runtimes',
    fold: {open: !row.folded, toggle: fold},
    count: row.agentIds.length,
    press: fold,
  };
}

/** The numbers over the tree: how many are working, how many in all, how many failed, the most at once. */
function Stats(props: {readonly tree: SpawnTree}): JSX.Element {
  const {stats} = props.tree;
  return (
    <>
      <Chip label={`${stats.running} running`} tone={stats.running > 0 ? 'live' : 'neutral'} dot />
      <Text variant="small" tone="muted">{`${stats.total} total`}</Text>
      <Text variant="small" tone="muted">{`${stats.failed} failed`}</Text>
      <Text variant="small" tone="muted">{`peak ${stats.peak}`}</Text>
    </>
  );
}

/**
 * The agents of a run as a page of the run: a spawn tree over time. A row is one agent from its
 * start to its end, under the agent that spawned it; a tick on a parent is a result that came back
 * to it; a count bar is agents folded into one row, and a click on it unfolds them. A click on an
 * agent opens its page. A key above the tree says what its bars, ticks and stacks mean.
 */
export function AgentsScreen(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  /** What the machine the run is on is called. */
  readonly host: string;
  readonly nowMs: number;
  /** Where the reader is: the levels above this screen. */
  readonly trail: readonly Crumb[];
  /** Where the read of the run's history stands, beside the counts. */
  readonly reading: ReactNode;
  readonly open: (agentId: string) => void;
}): JSX.Element {
  const clock = useDisplayClock();
  const [folds, setFolds] = useState<FoldChoices>({});
  const tree = spawnTree(props.state, props.runId, {
    nowMs: props.nowMs,
    host: props.host,
    folds,
  });
  const names = agentNames(props.state, props.runId);
  const labels = new Map([...names].map(([id, name]) => [id, agentLabel(name)] as const));
  const actions: Actions = {
    open: props.open,
    fold: target => setFolds(choices => toggled(choices, target)),
  };
  const rows = tree.rows.map(row =>
    row.kind === 'agent' ? agentRow(row, labels, actions) : groupRow(row, actions),
  );
  const to = Math.max(tree.endMs, tree.startMs + 1000);
  return (
    <Page
      key="agents"
      width="wide"
      state="ready"
      trail={props.trail}
      title="Agents"
      meta={
        <>
          <Chip label={props.host} mono clip />
          <Stats tree={tree} />
          {props.reading}
        </>
      }
    >
      <Card flush>
        <Gantt
          label="Agents over time"
          from={tree.startMs}
          to={to}
          axis={{label: clock.axisLabel, zone: clock.offsetMs}}
          rows={rows}
          nameLabel="Agent"
          tagLabel="Runtime"
          tagPriority="primary"
          figureLabel="Cost"
          detail="full"
          legend={LEGEND}
        />
      </Card>
    </Page>
  );
}
