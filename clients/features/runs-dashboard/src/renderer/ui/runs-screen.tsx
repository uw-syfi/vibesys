import {
  AttentionCard,
  Banner,
  Button,
  Card,
  Chip,
  EmptyState,
  Field,
  LoadingBar,
  type MenuItem,
  OverviewGroup,
  OverviewTile,
  Page,
  Row,
  Select,
  type SelectOption,
  Sparkline,
  SparklineKey,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  CHART_KEY,
  GROUP_BYS,
  GROUP_LABEL,
  GROUP_WORDS,
  type GroupBy,
  groupTiles,
  type NeedsYou,
  noRunsWord,
  onMachine,
  RUN_AGAIN,
  type Tile,
} from '../../state/index.js';

/** An unfinished New run, as the dashboard offers it: what it is of so far, and where it stands. */
export interface RunsDraft {
  readonly title: string;
  readonly detail: string;
  readonly open: () => void;
  readonly discard: () => void;
}

/** What a tile or card asks of the app, each handed the tile's id. */
interface TileActions {
  readonly open: (id: string) => void;
  /** Run an ended run again, from its place and options, in New run. */
  readonly again: (id: string) => void;
  readonly menu: (id: string) => readonly MenuItem[];
}

const GROUP_OPTIONS: readonly SelectOption<GroupBy>[] = GROUP_BYS.map(by => ({
  value: by,
  label: GROUP_WORDS[by],
}));

const KEY = (
  <SparklineKey
    label={CHART_KEY.label}
    items={[
      {mark: 'line', label: CHART_KEY.line},
      {mark: 'reference', label: CHART_KEY.reference},
    ]}
  />
);

function menuOf(actions: TileActions, id: string): {readonly menu?: readonly MenuItem[]} {
  const menu = actions.menu(id);
  return menu.length === 0 ? {} : {menu};
}

function TileOf(props: {readonly tile: Tile; readonly actions: TileActions}): JSX.Element {
  const {tile, actions} = props;
  return (
    <OverviewTile
      title={tile.title}
      hint={tile.hint}
      facts={tile.facts}
      result={tile.result}
      chart={
        tile.chart === null ? null : (
          <Sparkline
            label={tile.chart.label}
            steps={tile.chart.steps}
            from={tile.chart.from}
            to={tile.chart.to}
            reference={tile.chart.base}
          />
        )
      }
      status={{tone: tile.status.tone, label: tile.status.label}}
      summary={tile.summary}
      summaryHint={tile.summaryHint}
      {...menuOf(actions, tile.id)}
      press={() => actions.open(tile.id)}
    />
  );
}

function CardOf(props: {
  readonly tile: Tile;
  readonly needs: NeedsYou;
  readonly actions: TileActions;
}): JSX.Element {
  const {tile, needs, actions} = props;
  return (
    <AttentionCard
      title={tile.title}
      hint={tile.hint}
      facts={needs.facts}
      status={{tone: needs.tone, label: needs.label}}
      why={needs.why}
      actions={needs.again ? [{text: RUN_AGAIN, press: () => actions.again(tile.id)}] : []}
      {...menuOf(actions, tile.id)}
      press={() => actions.open(tile.id)}
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
 * The Runs dashboard: a header row with the title and a Group by select, then the runs that need
 * their user as cards that say why (and offer to run an ended one again), then every other run's
 * tile in groups on a grid that fills the width, with the key to their charts at the head of the
 * first group; each run is in one group. With no run it says so, with a New run action, or while
 * the machines are still answering (`reading`) that it is reading. Pressing a tile or card opens
 * its run (`open` is handed the tile's id); `menu` is a tile's row menu.
 */
export function RunsScreen(
  props: TileActions & {
    readonly tiles: readonly Tile[];
    readonly by: GroupBy;
    /** The machine the runs are narrowed to (as a tile names it), shown as a chip; null for all. */
    readonly machine: string | null;
    readonly clearMachine: () => void;
    readonly group: (by: GroupBy) => void;
    readonly draft: RunsDraft | null;
    /** Whether the machines have not yet said what runs they hold. */
    readonly reading: boolean;
    readonly newRun: () => void;
    /** What to say when saved runs could not be read; none when all were. */
    readonly notice: {readonly title: string; readonly body: string} | null;
  },
): JSX.Element {
  const {by, machine} = props;
  const tiles = onMachine(props.tiles, machine);
  const groups = groupTiles(tiles, by);
  const keyed = groups.find(group =>
    group.tiles.some(tile => tile.chart !== null && tile.attention === null),
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
              <Field label={GROUP_LABEL} inline>
                <Select value={by} options={GROUP_OPTIONS} change={props.group} size="sm" />
              </Field>
            )}
          </Row>
        </Row>
      }
    >
      <Stack gap="space-6">
        {props.notice === null ? null : (
          <Banner tone="warn" title={props.notice.title} body={props.notice.body} />
        )}
        {props.draft === null ? null : <Draft draft={props.draft} />}
        {tiles.length > 0 ? null : props.reading ? (
          <LoadingBar label="Reading runs" />
        ) : (
          <EmptyState
            title={noRunsWord(machine)}
            action={<Button variant="secondary" text="New run" press={props.newRun} />}
          />
        )}
        {groups.map(group => (
          <OverviewGroup
            key={group.key}
            title={group.title}
            {...(group.count === null ? {} : {count: group.count})}
            {...(group.tone === null ? {} : {tone: group.tone})}
            {...(group === keyed ? {aside: KEY} : {})}
            wide={group.tiles.every(tile => tile.attention !== null)}
          >
            {group.tiles.map(tile =>
              tile.attention === null ? (
                <TileOf key={tile.id} tile={tile} actions={props} />
              ) : (
                <CardOf key={tile.id} tile={tile} needs={tile.attention} actions={props} />
              ),
            )}
          </OverviewGroup>
        ))}
      </Stack>
    </Page>
  );
}
