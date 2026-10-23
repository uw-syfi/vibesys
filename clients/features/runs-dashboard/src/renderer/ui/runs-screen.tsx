import {
  Banner,
  Button,
  Card,
  type ChartKeyItem,
  Chip,
  EmptyState,
  Field,
  LoadingBar,
  type MenuItem,
  OverviewGroup,
  OverviewTile,
  Page,
  ResultChart,
  ResultChartKey,
  Row,
  Select,
  type SelectOption,
  Stack,
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
  {value: 'task', label: 'Task'},
  {value: 'agent', label: 'Agent'},
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
 * The Runs dashboard: a header row with the title, the chart key and a Group by select, then every
 * run's tile in groups on a grid that fills the width, the runs that need their user first, each
 * run in one group. With no run it says so, with a New run action, or while the machines are
 * still answering (`reading`) that it is reading. Pressing a tile opens its run (`open` is handed
 * the tile's id); `menu` is a tile's row menu.
 */
export function RunsScreen(props: {
  readonly tiles: readonly Tile[];
  readonly by: GroupBy;
  /** The machine the runs are narrowed to (as a tile names it), shown as a chip; null for all. */
  readonly machine: string | null;
  readonly clearMachine: () => void;
  readonly group: (by: GroupBy) => void;
  readonly open: (id: string) => void;
  readonly menu: (id: string) => readonly MenuItem[];
  readonly draft: RunsDraft | null;
  /** Whether the machines have not yet said what runs they hold. */
  readonly reading: boolean;
  readonly newRun: () => void;
  /** What to say when saved runs could not be read; none when all were. */
  readonly notice: {readonly title: string; readonly body: string} | null;
}): JSX.Element {
  const {by, machine} = props;
  const tiles = onMachine(props.tiles, machine);
  const tile = (each: Tile): JSX.Element => (
    <TileOf key={each.id} tile={each} open={props.open} menu={props.menu} />
  );
  return (
    <Page
      width="fill"
      header={
        <Row justify="between" gap="space-4" wrap>
          <Text as="h1" variant="title">
            Runs
          </Text>
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
              <>
                <ResultChartKey label="Chart key" items={CHART_KEY} />
                <Field label="Group by" hideLabel>
                  <Select value={by} options={GROUP_OPTIONS} change={props.group} size="sm" />
                </Field>
              </>
            )}
          </Row>
        </Row>
      }
    >
      <Stack gap="space-5">
        {props.notice === null ? null : (
          <Banner tone="warn" title={props.notice.title} body={props.notice.body} />
        )}
        {props.draft === null ? null : <Draft draft={props.draft} />}
        {tiles.length > 0 ? null : props.reading ? (
          <LoadingBar label="Reading runs" />
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
