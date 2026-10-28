import {
  AttentionCard,
  OverviewGroup,
  OverviewTile,
  RailFrame,
  RailItem,
  Sparkline,
  SparklineKey,
  type SparkStep,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const noop = (): void => undefined;

const STEPS: readonly SparkStep[] = [
  {x: 0, y: 1.12},
  {x: 3, y: 1.31},
  {x: 5, y: 1.42},
];

function Chart(props: {readonly steps?: readonly SparkStep[]}): JSX.Element {
  return (
    <Sparkline
      label="Best so far over 7 ideas, against the input"
      steps={props.steps ?? STEPS}
      from={0}
      to={7}
      reference={1}
    />
  );
}

const KEY = (
  <SparklineKey
    label="Chart key"
    items={[
      {mark: 'line', label: 'best so far'},
      {mark: 'reference', label: 'input'},
    ]}
  />
);

function Tiles(props: {readonly title: string}): JSX.Element {
  return (
    <OverviewGroup title={props.title} aside={KEY}>
      <OverviewTile
        title="queue-rs · mpmc"
        facts="host-a · Oct 17 04:00"
        result={{text: '1.42×', tone: 'ok', caption: 'best vs input'}}
        chart={<Chart />}
        status={{tone: 'live', label: 'running'}}
        summary="3 ideas working · 7 tried"
        menu={[{id: 'archive', label: 'Archive', press: noop}]}
        press={noop}
      />
      <OverviewTile
        title="a run whose title is longer than its tile is wide"
        facts="host-b · Oct 16 03:00"
        result={null}
        chart={<Chart steps={[]} />}
        status={{tone: 'warn', label: 'paused'}}
        summary="Paused by you · 2 ideas tried"
        press={noop}
      />
      <OverviewTile
        title="spsc"
        facts="host-b"
        result={{text: '287', tone: 'muted', caption: 'tok/s, best'}}
        chart={null}
        status={{tone: 'neutral', label: 'stopped'}}
        summary="Stopped · 1 idea in 12m"
        summaryHint={'Stopped · 1 idea in 12m\nThe machine went away during the second round'}
        press={noop}
      />
    </OverviewGroup>
  );
}

function Cards(): JSX.Element {
  return (
    <OverviewGroup title="Needs you" count={2} tone="err" wide>
      <AttentionCard
        title="queue-rs · spsc"
        facts="host-a · Oct 17 00:00 · 3 ideas · 40m"
        status={{tone: 'err', label: 'Failed'}}
        why="All 3 ideas failed, so nothing beat the input."
        actions={[{text: 'Run again with changes', press: noop}]}
        menu={[{id: 'archive', label: 'Archive', press: noop}]}
        press={noop}
      />
      <AttentionCard
        title="queue-rs · mpmc"
        facts="host-b · Oct 16 21:00 · 2 ideas · best 1.30×"
        status={{tone: 'warn', label: 'Quiet 32m'}}
        why="Still running, but nothing has been recorded for 32m. A long benchmark keeps writing output, so this much silence is worth a look, and this sentence runs past three lines to show the clamp."
        actions={[]}
        press={noop}
      />
    </OverviewGroup>
  );
}

export const OVERVIEW_ENTRIES: Pick<
  Record<ComponentName, GalleryEntry>,
  'RailFrame' | 'RailItem' | 'SparklineKey' | 'OverviewTile' | 'AttentionCard' | 'OverviewGroup'
> = {
  RailFrame: {
    render: () => (
      <RailFrame
        label="Rail"
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
  SparklineKey: {render: () => KEY},
  OverviewTile: {render: () => <Tiles title="Running 2 · Paused 1 · Stopped 1" />},
  AttentionCard: {render: () => <Cards />},
  OverviewGroup: {
    render: () => (
      <Stack>
        <Cards />
        <Tiles title="Running 2 · Paused 1 · Stopped 1" />
      </Stack>
    ),
  },
};
