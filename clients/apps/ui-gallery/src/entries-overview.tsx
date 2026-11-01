import {
  OverviewGroup,
  OverviewTile,
  RailFrame,
  RailItem,
  ResultChart,
  ResultChartKey,
  type SparkPoint,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const noop = (): void => undefined;

const POINTS: readonly SparkPoint[] = [
  {y: 1.12, mark: 'kept'},
  {y: 0.96, mark: 'not-better'},
  {y: 1, mark: 'failed'},
  {y: 1.31, mark: 'kept'},
  {y: 1.18, mark: 'not-better'},
  {y: 1.42, mark: 'kept'},
  {y: 1.42, mark: 'pending'},
];
const LINE = [1.12, 1.12, 1.12, 1.31, 1.31, 1.42, 1.42];

function Chart(props: {
  readonly points?: readonly SparkPoint[];
  readonly line?: readonly number[];
}): JSX.Element {
  return (
    <ResultChart
      label="Results of 7 ideas against the base, with the best so far"
      points={props.points ?? POINTS}
      line={props.line ?? LINE}
      reference={1}
    />
  );
}

function Tiles(props: {readonly title: string}): JSX.Element {
  return (
    <OverviewGroup title={props.title} count={2} tone="live">
      <OverviewTile
        title="mpmc"
        result={{text: '1.42×', tone: 'ok'}}
        facts="host-a · 8 cores · Codex"
        chart={<Chart />}
        figures="7 ideas · $4.20 · 1h 05m"
        note={null}
        status={{tone: 'live', label: 'running'}}
        menu={[{id: 'archive', label: 'Archive', press: noop}]}
        press={noop}
      />
      <OverviewTile
        title="spsc"
        result={{text: '0.91×', tone: 'muted'}}
        facts="host-b"
        chart={<Chart points={POINTS.slice(0, 3)} line={LINE.slice(0, 3)} />}
        figures="3 ideas · 12m"
        note="3 failed in a row"
        status={{tone: 'err', label: 'failed'}}
        attention
        press={noop}
      />
    </OverviewGroup>
  );
}

export const OVERVIEW_ENTRIES: Pick<
  Record<ComponentName, GalleryEntry>,
  'RailFrame' | 'RailItem' | 'ResultChart' | 'ResultChartKey' | 'OverviewTile' | 'OverviewGroup'
> = {
  RailFrame: {
    render: () => (
      <RailFrame
        label="Rail"
        state="ready"
        top={
          <>
            <RailItem icon="mark" label="Home" hideLabel press={noop} />
            <RailItem icon="plus" label="New" press={noop} />
            <RailItem
              icon="grid"
              label="Runs"
              active
              badge={{count: 2, label: '2 need you'}}
              press={noop}
            />
          </>
        }
        bottom={<RailItem icon="gear" label="Settings" press={noop} />}
      >
        <Text>The main area holds a Page.</Text>
      </RailFrame>
    ),
  },
  RailItem: {
    render: () => (
      <Stack>
        <RailItem icon="grid" label="Runs" active press={noop} />
        <RailItem icon="grid" label="Runs" badge={{count: 3, label: '3 need you'}} press={noop} />
        <RailItem icon="monitor" label="Machines" warning="A machine is unreachable" press={noop} />
        <RailItem icon="usage" label="Usage" press={noop} />
      </Stack>
    ),
  },
  ResultChart: {
    render: () => (
      <Stack>
        <Chart />
        <Chart points={[]} line={[]} />
        <Chart points={[{y: 1, mark: 'pending'}]} line={[1]} />
      </Stack>
    ),
  },
  ResultChartKey: {
    render: () => (
      <ResultChartKey
        label="Chart key"
        items={[
          {mark: 'kept', label: 'Kept'},
          {mark: 'not-better', label: 'Not better'},
          {mark: 'failed', label: 'Failed'},
          {mark: 'pending', label: 'Running'},
          {mark: 'line', label: 'Best so far'},
        ]}
      />
    ),
  },
  OverviewTile: {render: () => <Tiles title="Tiles" />},
  OverviewGroup: {render: () => <Tiles title="Running" />},
};
