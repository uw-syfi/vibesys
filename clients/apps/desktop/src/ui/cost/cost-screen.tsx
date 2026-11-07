import {
  BarRows,
  Button,
  Card,
  Chip,
  Meter,
  Row,
  Stack,
  StackedBars,
  Stat,
  SwatchLegend,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CostView, FeedStatus, Freshness} from '../../state/index.js';
import {faultWord} from '../orchestrator/words.js';
import {type CostScreenModel, costScreenModel} from './cost-model.js';
import {formatAxisUsd, NOT_REPORTED} from './words.js';

type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

/** The word and tone of the header status: how far to trust the figures on the screen. */
function statusOf(freshness: Freshness, runOver: boolean): {label: string; tone: Tone} {
  switch (freshness) {
    case 'live':
      return runOver ? {label: 'Final', tone: 'ok'} : {label: 'Live', tone: 'live'};
    case 'catching-up':
      return {label: 'Catching up', tone: 'neutral'};
    case 'stale':
      return {label: 'Stale', tone: 'warn'};
    case 'empty':
      return {label: 'Loading', tone: 'neutral'};
    case 'unavailable':
      return {label: 'Unavailable', tone: 'err'};
  }
}

/** The header of the screen: its status, and, beside it, what went wrong with the figures. */
function Header(props: {
  readonly freshness: Freshness;
  readonly status: FeedStatus;
  readonly runOver: boolean;
  readonly retry: () => void;
}): JSX.Element {
  const {label, tone} = statusOf(props.freshness, props.runOver);
  const failed = props.status._tag === 'Failed' ? props.status.fault : null;
  return (
    <Row gap="space-3" wrap justify="between">
      <Chip label={label} tone={tone} dot />
      {failed === null ? null : (
        <Row gap="space-3" wrap>
          <Text tone="warn" variant="small">
            {faultWord(failed.kind)}
          </Text>
          {failed.retryable ? (
            <Button
              text="Retry"
              label="Retry cost"
              variant="secondary"
              size="sm"
              press={props.retry}
            />
          ) : null}
        </Row>
      )}
    </Row>
  );
}

function SpendCard(props: {readonly spend: CostScreenModel['spend']}): JSX.Element {
  const {spend} = props;
  const figure = `${spend.floor ? '≥ ' : ''}${spend.figure ?? NOT_REPORTED}`;
  return (
    <Card section="Spend">
      <Stack gap="space-4">
        <Row gap="space-4" align="baseline" wrap>
          <Text variant="hero" tone={spend.figure === null ? 'muted' : 'default'}>
            {figure}
          </Text>
          {spend.cap === null ? null : (
            <Text variant="heading" tone="muted">{`of ${spend.cap}`}</Text>
          )}
        </Row>
        {spend.capShare === null ? null : (
          <Meter label={`${figure} of ${spend.cap ?? ''}`} value={spend.capShare} />
        )}
        <Row gap="space-7" rowGap="space-4" wrap>
          <Stat
            label="Last hour"
            value={spend.lastHour ?? NOT_REPORTED}
            quiet={spend.lastHour === null}
          />
          <Stat label="Turns" value={spend.turns} />
          <Stat label="Unpriced turns" value={spend.unpricedTurns} />
        </Row>
      </Stack>
    </Card>
  );
}

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
          columns={[{header: 'Agents'}, {header: 'Turns'}, {header: 'Cost', strong: true}]}
          rows={model.types}
        />
      </Card>
      <Row gap="space-4" align="start" wrap>
        <Stack gap="space-4" width="chart">
          <Card section="By model">
            <BarRows
              label="Cost by model"
              nameHeader="Model"
              barLabel="Share of the largest cost"
              columns={[{header: 'Turns'}, {header: 'Cost', strong: true}]}
              rows={model.models}
            />
          </Card>
        </Stack>
        <Stack gap="space-4" width="chart">
          <Card section="Tokens by kind">
            <BarRows
              label="Tokens by kind"
              nameHeader="Kind"
              barLabel="Share of the largest count"
              columns={[{header: 'Tokens', strong: true}]}
              rows={model.kinds}
            />
          </Card>
        </Stack>
      </Row>
    </>
  );
}

/**
 * One run's cost, as the host served it: what it has cost against its cap, the spend over time by
 * model, and its split by agent type, by model and by kind of token. The screen computes none of
 * it. With no view yet it shows only its status, and with a stream the host cannot serve, why.
 */
export function CostScreen(props: {
  readonly view: CostView | undefined;
  readonly freshness: Freshness;
  readonly status: FeedStatus;
  /** The run has ended, so its view is final. */
  readonly runOver: boolean;
  readonly retry: () => void;
}): JSX.Element {
  const header = (
    <Header
      freshness={props.freshness}
      status={props.status}
      runOver={props.runOver}
      retry={props.retry}
    />
  );
  if (props.view === undefined) return header;
  const model = costScreenModel(props.view);
  return (
    <Stack gap="space-4">
      {header}
      <SpendCard spend={model.spend} />
      {model.empty ? (
        <Text tone="muted">No turns yet.</Text>
      ) : (
        <>
          {model.chart === null ? null : <SpendOverTime chart={model.chart} />}
          <Splits model={model} />
        </>
      )}
    </Stack>
  );
}
