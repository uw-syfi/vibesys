import type {RunTrace} from '@vibesys/run-state/trace';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  KeyValue,
  Row,
  SegmentedControl,
  Split,
  Stack,
  Text,
  Tree,
  TreeRow,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type LogStreamItem,
  MAP_FILTERS,
  type MapDetail,
  type MapFilter,
  type MapRow,
  type MapState,
  mapDetail,
  mapRows,
} from '../../state/index.js';

const FILTER_LABELS: Readonly<Record<MapFilter, string>> = {
  all: 'All',
  running: 'Running',
  waiting: 'Waiting',
  failed: 'Failed',
  finished: 'Finished',
};

/** What the Map asks of the screen: the page's actions on its filter, groups and selection. */
export interface MapActions {
  readonly filter: (filter: MapFilter) => void;
  readonly toggle: (key: string, open: boolean) => void;
  readonly select: (spanId: string) => void;
  /** Open a log stream in the Logs tab. */
  readonly openLog: (streamId: string) => void;
}

function MapTree(props: {
  readonly rows: readonly MapRow[];
  readonly selected: string | null;
  readonly actions: MapActions;
}): JSX.Element {
  const {rows, actions} = props;
  // A selection the rows do not show (inside a closed group) leaves the tree itself as its tab stop.
  const selected = rows.some(row => row.id === props.selected) ? props.selected : null;
  return (
    <Tree label="Run steps" {...(selected === null ? {} : {selected})}>
      {rows.map(row =>
        row.kind === 'group' ? (
          <TreeRow
            key={row.id}
            id={row.id}
            depth={row.depth}
            title={row.title}
            subtitle={row.subtitle}
            expanded={row.open}
            toggle={open => actions.toggle(row.key, open)}
            select={() => undefined}
          />
        ) : (
          <TreeRow
            key={row.id}
            id={row.id}
            depth={row.depth}
            title={row.title}
            subtitle={row.subtitle}
            state={row.state}
            chips={row.chips}
            select={() => actions.select(row.id)}
          />
        ),
      )}
    </Tree>
  );
}

function Detail(props: {
  readonly detail: MapDetail;
  readonly logs: readonly LogStreamItem[];
  readonly actions: MapActions;
}): JSX.Element {
  const {detail, logs, actions} = props;
  return (
    <Card section={detail.kind}>
      <Stack gap="space-3">
        <Text as="h3" variant="heading">
          {detail.title}
        </Text>
        <Row>
          <Chip label={detail.state.label} tone={detail.state.tone} dot />
        </Row>
        <KeyValue items={detail.facts} />
        {detail.links.length === 0 ? null : (
          <Row gap="space-2" wrap>
            {detail.links.map(link => (
              <Button
                key={`${link.label} ${link.to}`}
                size="sm"
                variant="quiet"
                text={link.label}
                press={() => actions.select(link.to)}
              />
            ))}
          </Row>
        )}
        <Text variant="section">Logs</Text>
        {logs.length === 0 ? (
          <Text tone="muted">No logs for this step yet.</Text>
        ) : (
          <Row gap="space-2" wrap>
            {logs.map(stream => (
              <Button
                key={stream.id}
                size="sm"
                variant="secondary"
                text={stream.label}
                press={() => actions.openLog(stream.id)}
              />
            ))}
          </Row>
        )}
      </Stack>
    </Card>
  );
}

/**
 * The Map tab: the run's steps as a tree at the scrubber's view, a state filter, and the selected
 * step's details with the logs its owner names. `trace` is null until the run's trace reaches the
 * window; `logsOf` gives the log streams a span owns.
 */
export function MapTab(props: {
  readonly trace: RunTrace | null;
  readonly map: MapState;
  readonly logsOf: (spanId: string) => readonly LogStreamItem[];
  readonly actions: MapActions;
}): JSX.Element {
  const {trace, map, actions} = props;
  const rows = trace === null ? [] : mapRows(trace, map);
  const detail = trace === null ? null : mapDetail(trace, map);
  const filter = (
    <SegmentedControl
      label="Show steps"
      options={MAP_FILTERS.map(value => ({value, label: FILTER_LABELS[value]}))}
      value={map.filter}
      change={actions.filter}
      size="sm"
    />
  );
  if (detail === null) {
    return (
      <Stack gap="space-4">
        {filter}
        <Card>
          <EmptyState
            title="No steps yet"
            body={
              trace === null
                ? 'The run’s steps have not reached this window yet.'
                : 'The run’s planning calls, workstreams, agent turns and gates appear here as it starts them.'
            }
          />
        </Card>
      </Stack>
    );
  }
  return (
    <Stack gap="space-4">
      {filter}
      <Split
        main={
          <Card>
            <MapTree rows={rows} selected={detail.id} actions={actions} />
          </Card>
        }
        aside={<Detail detail={detail} logs={props.logsOf(detail.id)} actions={actions} />}
      />
    </Stack>
  );
}
