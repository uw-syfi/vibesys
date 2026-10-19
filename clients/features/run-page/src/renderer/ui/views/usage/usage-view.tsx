/**
 * The Usage face: what the run's agent turns spent. Above, the run's totals (the header's words
 * for its tokens, then cost and turns), the controls (what the chart shows, and how the table
 * groups), the spend over time as stacked bars by the first group, and a table with a row per
 * group. Every figure is a fold of the run's usage records (`usageView`), so they agree.
 */
import {
  EmptyState,
  Fact,
  FactLine,
  Field,
  Row,
  SegmentedControl,
  Select,
  Stack,
  StackedBars,
  Swatch,
  SwatchLegend,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {Fragment, type JSX} from 'react';
import {
  groupedBy,
  USAGE_GROUPS,
  USAGE_METRICS,
  type UsageControls,
  type UsageRecord,
  type UsageRowView,
  usageView,
} from '../../../../state/index.js';

export interface UsageViewProps {
  readonly records: readonly UsageRecord[];
  readonly controls: UsageControls;
  readonly change: (controls: UsageControls) => void;
}

const COLUMNS: readonly TableColumn<UsageRowView>[] = [
  {
    key: 'group',
    header: 'group',
    cell: row => (
      <Row gap="space-2" align="center">
        <Swatch series={row.swatch} />
        {row.label.map((part, at) => (
          <Fragment key={part.text}>
            {at === 0 ? null : (
              <Text variant="small" tone="muted">
                ·
              </Text>
            )}
            <Text variant={part.id ? 'code' : 'body-strong'}>{part.text}</Text>
          </Fragment>
        ))}
      </Row>
    ),
  },
  {key: 'input', header: 'input', cell: row => row.input, mono: true, align: 'end'},
  {key: 'cached', header: 'cached', cell: row => row.cached, mono: true, align: 'end'},
  {key: 'output', header: 'output', cell: row => row.output, mono: true, align: 'end'},
  {key: 'cost', header: 'cost', cell: row => row.cost, mono: true, align: 'end'},
  {key: 'turns', header: 'turns', cell: row => row.turns, mono: true, align: 'end'},
  {key: 'share', header: 'share', cell: row => row.share, mono: true, align: 'end'},
];

export function UsageView(props: UsageViewProps): JSX.Element {
  const {controls, change} = props;
  const view = usageView(props.records, controls);
  if (view.empty) {
    return <EmptyState title="No usage yet" body="Each agent turn's tokens and cost show here." />;
  }
  const thenOptions = [
    {value: 'none' as const, label: 'None'},
    ...USAGE_GROUPS.filter(group => group.value !== controls.by),
  ];
  return (
    <Stack gap="space-3">
      <FactLine>
        {view.spent === null ? null : (
          <Fact title={view.spent.title}>
            <Text variant="code" tone="muted">
              {view.spent.label}
            </Text>
          </Fact>
        )}
        <Fact>
          <Text variant="code" tone="muted">{`· cost ${view.cost}`}</Text>
        </Fact>
        <Fact>
          <Text variant="code" tone="muted">{`· ${view.turns} turns`}</Text>
        </Fact>
      </FactLine>
      <Row gap="space-3" align="end" wrap>
        <Field label="Group by">
          <Select
            size="sm"
            value={controls.by}
            options={USAGE_GROUPS}
            change={by => change(groupedBy(controls, by))}
          />
        </Field>
        <Field label="Then by">
          <Select
            size="sm"
            value={controls.within ?? 'none'}
            options={thenOptions}
            change={within => change({...controls, within: within === 'none' ? null : within})}
          />
        </Field>
        <SegmentedControl
          label="Chart shows"
          size="sm"
          options={USAGE_METRICS}
          value={controls.metric}
          change={metric => change({...controls, metric})}
        />
      </Row>
      {view.span === null ? null : (
        <StackedBars
          label="Usage over time"
          unit={view.unit}
          bars={view.bars}
          from={view.span.from}
          to={view.span.to}
        />
      )}
      <SwatchLegend
        label="Groups"
        items={view.legend.map(item => ({label: item.label, series: item.series, mono: item.id}))}
      />
      {view.undated === 0 ? null : (
        <Text variant="small" tone="muted">
          {`${view.undated} turns have no end time and are left out of the chart.`}
        </Text>
      )}
      <Table label="Usage by group" columns={COLUMNS} rows={view.rows} rowKey={row => row.id} />
    </Stack>
  );
}
