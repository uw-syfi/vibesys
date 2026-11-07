import {Card, Chip, Gantt, type GanttRow, type Guide, Row, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
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
import {agentLabel, groupLabel} from './labels.js';
import {axisLabel, formatSpan, formatUsd} from './time.js';

const TICKS = 5;

/** The lineage cells before a row's name: a line through each ancestor that has later siblings, a branch for the row. */
export function guidesOf(lineage: readonly Branch[]): Guide[] {
  return lineage.map((branch, level): Guide => {
    if (level < lineage.length - 1) return branch === 'more' ? 'pipe' : 'blank';
    return branch === 'more' ? 'tee' : 'elbow';
  });
}

const KIND = {running: 'solid', done: 'grey', failed: 'err'} as const;

function ticksOf(from: number, to: number) {
  return Array.from({length: TICKS + 1}, (_, index) => {
    const at = from + ((to - from) * index) / TICKS;
    return {at, label: axisLabel(at, to - from)};
  });
}

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
    tag: row.host,
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

function agentRow(
  row: AgentRow,
  labels: ReadonlyMap<string, string>,
  selected: string | null,
  actions: Actions,
): GanttRow {
  const fold = () => actions.fold(row);
  const count = foldedCount(row);
  return {
    ...shared(row, labels.get(row.agentId) ?? ''),
    ...(row.costUsd === null ? {} : {figure: formatUsd(row.costUsd)}),
    ...(row.children > 0 ? {fold: {open: !isFolded(row), toggle: fold}} : {}),
    ...(count > 0 ? {count} : {}),
    marks: row.ticks.map(tick => ({
      at: tick.atMs,
      tone: tick.tone === 'failed' ? 'err' : 'ok',
      label: labels.get(tick.childId) ?? '',
    })),
    selected: row.agentId === selected,
    press: () => actions.open(row.agentId),
  };
}

function groupRow(row: GroupRow, actions: Actions): GanttRow {
  const fold = () => actions.fold(row);
  return {
    ...shared(row, groupLabel(row.role)),
    fold: {open: !row.folded, toggle: fold},
    count: row.agentIds.length,
    press: fold,
  };
}

/** The numbers over the tree: how many are working, how many in all, how many failed, the most at once. */
function Stats(props: {readonly tree: SpawnTree}): JSX.Element {
  const {stats} = props.tree;
  return (
    <Row gap="space-5" wrap align="center">
      <Chip label={`${stats.running} running`} tone="live" dot />
      <Text variant="small" tone="muted">{`${stats.total} total`}</Text>
      <Text variant="small" tone="muted">{`${stats.failed} failed`}</Text>
      <Text variant="small" tone="muted">{`peak ${stats.peak}`}</Text>
    </Row>
  );
}

/**
 * The agents of a run as a spawn tree over time. A row is one agent from its start to its end,
 * under the agent that spawned it; a tick on a parent is a result that came back to it; a count
 * bar is agents folded into one row, and a click on it unfolds them. A click on an agent opens it.
 */
export function AgentsScreen(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  /** What the machine the run is on is called. */
  readonly host: string;
  readonly nowMs: number;
  /** The agent open beside the tree, if any. */
  readonly selected: string | null;
  readonly open: (agentId: string) => void;
}): JSX.Element {
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
    row.kind === 'agent' ? agentRow(row, labels, props.selected, actions) : groupRow(row, actions),
  );
  const to = Math.max(tree.endMs, tree.startMs + 1000);
  return (
    <Stack gap="space-4">
      <Stats tree={tree} />
      <Card flush>
        <Gantt
          label="Agents over time"
          from={tree.startMs}
          to={to}
          ticks={ticksOf(tree.startMs, to)}
          rows={rows}
          nameLabel="Agent"
          tagLabel="Machine"
          figureLabel="Cost"
          detail="full"
        />
      </Card>
    </Stack>
  );
}
