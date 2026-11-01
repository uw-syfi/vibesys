import {
  Button,
  Card,
  type ChartKeyItem,
  Chip,
  EmptyState,
  Field,
  type MenuItem,
  OverviewGroup,
  OverviewTile,
  Page,
  type PageState,
  ResultChart,
  ResultChartKey,
  Row,
  Select,
  type SelectOption,
  Stack,
  StatusDot,
  type StepItem,
  StepList,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {BASE, type GroupBy, groupTiles, onMachine, type Tile} from '../../state/index.js';

/** An unfinished New run, as the dashboard offers it: what it is of so far, and where it stands. */
export interface RunsDraft {
  readonly title: string;
  readonly detail: string;
  readonly open: () => void;
  readonly discard: () => void;
}

const GROUP_OPTIONS: readonly SelectOption<GroupBy>[] = [
  {value: 'status', label: 'Status'},
  {value: 'machine', label: 'Machine'},
];

const CHART_KEY: readonly ChartKeyItem[] = [
  {mark: 'kept', label: 'Kept'},
  {mark: 'not-better', label: 'Not better'},
  {mark: 'failed', label: 'Failed'},
  {mark: 'pending', label: 'Running'},
  {mark: 'line', label: 'Best so far'},
];

function TileOf(props: {
  readonly tile: Tile;
  readonly open: (id: string) => void;
  readonly menu: (id: string) => readonly MenuItem[];
}): JSX.Element {
  const {tile} = props;
  const menu = props.menu(tile.id);
  return (
    <OverviewTile
      title={tile.title}
      hint={tile.hint}
      result={tile.result}
      facts={tile.facts}
      chart={
        tile.chart === null ? null : (
          <ResultChart
            label={tile.chart.label}
            points={tile.chart.points}
            line={tile.chart.line}
            reference={BASE}
          />
        )
      }
      figures={tile.figures}
      note={tile.note}
      noteFull={tile.noteFull}
      status={{tone: tile.status.tone, label: tile.status.label}}
      attention={tile.attention}
      {...(menu.length === 0 ? {} : {menu})}
      press={() => props.open(tile.id)}
    />
  );
}

/** One machine's way to its run list; `retry` is set once it has stopped. */
export interface MachineSteps {
  readonly machine: string;
  readonly steps: readonly StepItem[];
  readonly retry: (() => void) | null;
}

/**
 * One machine that has not listed, as a line among the tiles: where it stands (the step it is on,
 * or the one it stopped at, with the reason) and, once stopped, a way to ask again.
 */
function MachineLine(props: {readonly load: MachineSteps}): JSX.Element {
  const {load} = props;
  const at = load.steps.find(step => step.state === 'current' || step.state === 'failed');
  const failed = at?.state === 'failed';
  return (
    <Row gap="space-2" wrap>
      <StatusDot tone={failed ? 'err' : 'live'} label={failed ? 'Failed' : 'Loading'} />
      <Text variant="body-strong">{load.machine}</Text>
      {at === undefined ? null : (
        <Text variant="small" tone="muted">
          {[at.label, at.position, at.note].filter(each => each !== undefined).join(' ')}
        </Text>
      )}
      {load.retry === null ? null : (
        <Button variant="quiet" size="sm" text="Retry" press={load.retry} />
      )}
    </Row>
  );
}

function Draft(props: {readonly draft: RunsDraft}): JSX.Element {
  const {draft} = props;
  return (
    <Card>
      <Row justify="between" wrap>
        <Row gap="space-4" wrap>
          <Text variant="section">Draft</Text>
          <Text variant="body-strong">{draft.title}</Text>
          <Text variant="small" tone="muted">
            {draft.detail}
          </Text>
        </Row>
        <Row>
          <Button variant="secondary" size="sm" text="Open" press={draft.open} />
          <Button variant="quiet" size="sm" text="Discard" press={draft.discard} />
        </Row>
      </Row>
    </Card>
  );
}

/**
 * The Runs dashboard: a header with the title and the chart key, and under it the machine the runs
 * are narrowed to (when one is) and a Group by select, then every run's tile in groups on a grid
 * that fills the width, the runs that need their user first, each run in one group. With no run and
 * every machine listed it says so, with a New run action; before that each machine still loading or
 * stopped has a block of its steps (`loads`), and once tiles show it keeps a line of where it
 * stands, so a machine that is missing is not silently absent. Pressing a tile opens its run
 * (`open` is handed the tile's id); `menu` is a tile's row menu.
 */
export function RunsScreen(props: {
  /** Whether the page waits for a reply (the runs of each machine); it is the page's `data-state`. */
  readonly settled: PageState;
  readonly tiles: readonly Tile[];
  readonly by: GroupBy;
  /** The machine the runs are narrowed to (as a tile names it), shown as a chip; null for all. */
  readonly machine: string | null;
  readonly clearMachine: () => void;
  readonly group: (by: GroupBy) => void;
  readonly open: (id: string) => void;
  readonly menu: (id: string) => readonly MenuItem[];
  readonly draft: RunsDraft | null;
  /** The machines that have not said what runs they hold, each with its steps. */
  readonly loads: readonly MachineSteps[];
  readonly newRun: () => void;
  /** What to say when saved runs could not be read; none when all were. */
  readonly notice: {readonly title: string; readonly body: string} | null;
}): JSX.Element {
  const {by, machine} = props;
  const tiles = onMachine(props.tiles, machine);
  const loads = props.loads.filter(load => machine === null || load.machine === machine);
  const tile = (each: Tile): JSX.Element => (
    <TileOf key={each.id} tile={each} open={props.open} menu={props.menu} />
  );
  return (
    <Page
      width="fill"
      state={props.settled}
      header={
        <Stack gap="space-4">
          <Row justify="between" gap="space-4" wrap>
            <Row gap="space-3" align="center" wrap>
              <Text as="h1" variant="title">
                Runs
              </Text>
              {props.notice === null ? null : (
                <Chip label={props.notice.title} tone="warn" dot hint={props.notice.body} />
              )}
            </Row>
            {tiles.length === 0 ? null : <ResultChartKey label="Chart key" items={CHART_KEY} />}
          </Row>
          {machine === null && tiles.length === 0 ? null : (
            <Row gap="space-5" wrap>
              {machine === null ? null : (
                <Row gap="space-1">
                  <Chip label={machine} />
                  <Button
                    variant="quiet"
                    size="sm"
                    icon="cross"
                    label={`Remove ${machine}`}
                    press={props.clearMachine}
                  />
                </Row>
              )}
              {tiles.length === 0 ? null : (
                <Field label="Group by" hideLabel>
                  <Select
                    labelInside
                    value={by}
                    options={GROUP_OPTIONS}
                    change={props.group}
                    size="sm"
                  />
                </Field>
              )}
            </Row>
          )}
        </Stack>
      }
    >
      <Stack gap="space-5">
        {props.draft === null ? null : <Draft draft={props.draft} />}
        {tiles.length > 0 ? (
          loads.length > 0 ? (
            <Stack gap="space-1">
              {loads.map(load => (
                <MachineLine key={load.machine} load={load} />
              ))}
            </Stack>
          ) : null
        ) : loads.length > 0 ? (
          loads.map(load => (
            <StepList
              key={load.machine}
              title={load.machine}
              steps={load.steps}
              {...(load.retry === null ? {} : {retry: load.retry})}
            />
          ))
        ) : (
          <EmptyState
            title="No runs"
            action={<Button variant="secondary" text="New run" press={props.newRun} />}
          />
        )}
        {groupTiles(tiles, by).map(group => (
          <OverviewGroup
            key={group.key}
            title={group.title}
            count={group.tiles.length}
            {...(group.tone === null ? {} : {tone: group.tone})}
          >
            {group.tiles.map(tile)}
          </OverviewGroup>
        ))}
      </Stack>
    </Page>
  );
}
