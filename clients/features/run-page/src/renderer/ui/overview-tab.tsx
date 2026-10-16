import {
  Banner,
  Card,
  Chart,
  EmptyState,
  Grid,
  KeyValue,
  type KeyValueItem,
  Prose,
  Split,
  Stack,
  Stat,
  StatusDot,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type ChartView,
  type CurrentWorkItem,
  type ObjectiveView,
  overviewView,
  type RunAtView,
  type RunPageState,
} from '../../state/index.js';

function Objective(props: {readonly objective: ObjectiveView | null}): JSX.Element {
  const {objective} = props;
  if (objective === null) {
    return (
      <Card section="Objective">
        <Text tone="muted">The run has not declared its objective yet.</Text>
      </Card>
    );
  }
  const facts: KeyValueItem[] = [
    ...(objective.metric === null ? [] : [{key: 'Judged on', value: objective.metric}]),
    ...(objective.check === null
      ? []
      : [{key: 'Correctness check', value: objective.check, mono: true}]),
    ...(objective.evaluator === null
      ? []
      : [{key: 'Evaluator', value: objective.evaluator, mono: true}]),
  ];
  return (
    <Card section="Objective">
      <Stack gap="space-4">
        <Prose blocks={objective.blocks} />
        {facts.length === 0 ? null : <KeyValue items={facts} />}
      </Stack>
    </Card>
  );
}

function Performance(props: {readonly chart: ChartView | null}): JSX.Element {
  const {chart} = props;
  if (chart === null) {
    return (
      <Card section="Performance">
        <EmptyState
          title="Nothing measured yet"
          body="Each measured candidate appears here, with the best so far and the baseline."
        />
      </Card>
    );
  }
  return (
    <Card section="Performance">
      <Stack gap="space-3">
        <Text variant="small" tone="muted">
          {chart.legend}
        </Text>
        <Chart
          label={chart.label}
          points={chart.points}
          steps={chart.steps}
          {...(chart.reference === null ? {} : {reference: chart.reference})}
          playhead={chart.playhead}
          xTicks={chart.xTicks}
          yTicks={chart.yTicks}
          xTitle={chart.xTitle}
        />
      </Stack>
    </Card>
  );
}

function CurrentWork(props: {readonly items: readonly CurrentWorkItem[]}): JSX.Element {
  return (
    <Card section="Current work">
      {props.items.length === 0 ? (
        <Text tone="muted">Nothing is running at this point of the run.</Text>
      ) : (
        <Stack gap="space-4">
          {props.items.map(item => (
            <Stack key={item.id} gap="space-1">
              <Text variant="body">
                <StatusDot tone={item.tone} label={item.state} /> {item.title}
              </Text>
              <Text variant="small" tone="muted">
                {item.doing}
              </Text>
            </Stack>
          ))}
        </Stack>
      )}
    </Card>
  );
}

/**
 * The Overview tab: the objective, the key numbers, the performance chart and the current work of
 * the run at the view (`run`), as `overviewView` reads them.
 */
export function OverviewTab(props: {
  readonly run: RunAtView;
  readonly state: RunPageState;
}): JSX.Element {
  const view = overviewView(props.run, props.state);
  return (
    <Stack gap="space-5">
      {view.progress === null ? null : (
        <Banner title={view.progress.title} body={view.progress.body} />
      )}
      <Objective objective={view.objective} />
      <Grid min="tile">
        {view.numbers.map(number => (
          <Card key={number.label}>
            <Stat label={number.label} value={number.value} sub={number.sub} />
          </Card>
        ))}
      </Grid>
      <Split
        main={<Performance chart={view.chart} />}
        aside={<CurrentWork items={view.current} />}
      />
    </Stack>
  );
}
