import {
  BarRows,
  Button,
  Card,
  ChartColumns,
  Chip,
  type Crumb,
  Page,
  pageState,
  Row,
  Stack,
  StackedBars,
  SwatchLegend,
  Text,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {CostView, FeedStatus, HostStatus} from '../../state/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {
  type CostScreenModel,
  costScreenModel,
  KIND_COLUMNS,
  MODEL_COLUMNS,
  TYPE_COLUMNS,
} from './cost-model.js';
import {SpendCard} from './spend-card.js';
import {type CostWords, formatAxisUsd} from './words.js';

/** What went wrong with the figures, with the way to try again when there is one. */
function Problem(props: {
  readonly status: FeedStatus;
  readonly words: CostWords;
  readonly retry: () => void;
}): JSX.Element | null {
  const failed = props.status._tag === 'Failed' ? props.status.fault : null;
  if (failed === null) return null;
  return (
    <Row gap="space-3" wrap>
      <Chip label={props.words.fault(failed.kind)} tone="warn" dot />
      {failed.retryable ? (
        <Button text="Retry" label="Retry cost" variant="secondary" size="sm" press={props.retry} />
      ) : null}
    </Row>
  );
}

/** The spend over time as bars by model, with the models' legend; the same for a run and for a host. */
function SpendOverTime(props: {
  readonly chart: NonNullable<CostScreenModel['chart']>;
}): JSX.Element {
  const {chart} = props;
  return (
    <Card section="Spend over time">
      <Stack gap="space-4">
        {chart.legend.length === 0 ? null : <SwatchLegend label="Models" items={chart.legend} />}
        <StackedBars
          label="Spend over time"
          unit="dollars"
          size="tall"
          bars={chart.bars}
          marks={chart.marks}
          valueFormat={formatAxisUsd}
        />
      </Stack>
    </Card>
  );
}

function Splits(props: {readonly model: CostScreenModel}): JSX.Element {
  const {model} = props;
  return (
    <>
      <Card section="By agent type">
        <BarRows
          label="Cost by agent type"
          nameHeader="Type"
          barLabel="Share of the largest cost"
          columns={TYPE_COLUMNS}
          rows={model.types}
        />
      </Card>
      <ChartColumns align="start">
        <Card section="By CLI provider and model">
          <BarRows
            label="Cost by CLI provider and model"
            nameHeader="Provider · model"
            barLabel="Share of the largest cost"
            columns={MODEL_COLUMNS}
            rows={model.models}
          />
        </Card>
        <Card section="Tokens by kind">
          <BarRows
            label="Tokens by kind"
            nameHeader="Kind"
            barLabel="Share of the largest count"
            columns={KIND_COLUMNS}
            rows={model.kinds}
          />
        </Card>
      </ChartColumns>
    </>
  );
}

/**
 * One run's cost, as the host served it, as a page of the run: what it has cost against its cap,
 * the spend over time by model, and its split by agent type, by model and by kind of token. The
 * screen computes none of it. With no view yet it shows only its status, and with a stream the host
 * cannot serve, why. The status sits in the title row's meta, where every run page puts its own.
 */
export function CostScreen(props: {
  readonly view: CostView | undefined;
  /** The host's one status, the same word every screen says of it. */
  readonly status: HostStatus;
  /** How the cost stream stands, for what went wrong with it. */
  readonly feed: FeedStatus;
  /** The run has ended, so its view is final. */
  readonly runOver: boolean;
  /** The words of the run's own vocabulary: a type of agent, a harness, a fault, a moment. */
  readonly words: CostWords;
  /** Where the reader is: the levels above this screen. */
  readonly trail: readonly Crumb[];
  /** Where the read of the run's history stands, beside the status. */
  readonly reading: ReactNode;
  readonly retry: () => void;
}): JSX.Element {
  const model = props.view === undefined ? undefined : costScreenModel(props.view, props.words);
  return (
    <Page
      key="cost"
      width="wide"
      state={pageState(
        props.view === undefined &&
          (props.status.kind === 'waiting' || props.status.kind === 'connecting'),
      )}
      trail={props.trail}
      title="Cost"
      meta={
        <>
          {props.runOver && props.status.kind === 'live' ? null : (
            <HostStatusChip status={props.status} />
          )}
          {props.reading}
        </>
      }
    >
      <Problem status={props.feed} words={props.words} retry={props.retry} />
      {model === undefined ? null : (
        <>
          <SpendCard spend={model.spend} runOver={props.runOver} />
          {model.empty ? (
            <Text tone="muted">No turns yet.</Text>
          ) : (
            <>
              {model.chart === null ? null : <SpendOverTime chart={model.chart} />}
              {model.modelsTruncated ? (
                <Chip label="Some provider and model rows are not shown" tone="warn" dot />
              ) : null}
              <Splits model={model} />
            </>
          )}
        </>
      )}
    </Page>
  );
}
