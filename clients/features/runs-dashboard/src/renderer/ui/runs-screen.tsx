import {
  Button,
  Card,
  Field,
  type MenuItem,
  OverviewGroup,
  OverviewTile,
  Page,
  ResultChart,
  Row,
  Select,
  type SelectOption,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {BASE, type GroupBy, groupTiles, needsYou, type Tile} from '../../state/index.js';

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
      now={tile.now}
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
 * The Runs dashboard, the app's home: a title and a Group by select, the "Needs you" row of the runs
 * that failed or stalled, then every run's tile in groups. With no run it offers New run alone.
 * Pressing a tile opens its run (`open` is handed the tile's id); `menu` is a tile's row menu.
 */
export function RunsScreen(props: {
  readonly tiles: readonly Tile[];
  readonly by: GroupBy;
  readonly group: (by: GroupBy) => void;
  readonly open: (id: string) => void;
  readonly menu: (id: string) => readonly MenuItem[];
  readonly newRun: () => void;
  readonly draft: RunsDraft | null;
}): JSX.Element {
  const {tiles, by} = props;
  const attention = needsYou(tiles);
  const tile = (each: Tile): JSX.Element => (
    <TileOf key={each.id} tile={each} open={props.open} menu={props.menu} />
  );
  return (
    <Page
      width="wide"
      header={
        <Row justify="between" wrap>
          <Text as="h1" variant="title">
            Runs
          </Text>
          {tiles.length === 0 ? null : (
            <Field label="Group by">
              <Select value={by} options={GROUP_OPTIONS} change={props.group} size="sm" />
            </Field>
          )}
        </Row>
      }
    >
      <Stack gap="space-6">
        {props.draft === null ? null : <Draft draft={props.draft} />}
        {tiles.length === 0 ? (
          <Row justify="center">
            <Button variant="primary" icon="plus" text="New run" press={props.newRun} />
          </Row>
        ) : (
          <>
            {attention.length === 0 ? null : (
              <OverviewGroup title="Needs you" count={attention.length} tone="err">
                {attention.map(tile)}
              </OverviewGroup>
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
          </>
        )}
      </Stack>
    </Page>
  );
}
