/**
 * The Usage face: what the run's agent turns spent, over the whole width of the screen. Above, four
 * tiles (cost, tokens, cached input, spend per idea); the controls (what the table groups by, what
 * the chart and the shares count, and whether the chart shows each slice of time or the running
 * total); the chart; its legend; and a table with a row per group that unfolds into a second
 * grouping and follows its series in the chart. Every figure is a fold of the run's usage records
 * (`usageView`), so they agree; a cost nobody reported is counted `unpriced` on the cost tile.
 */
import {
  Checkbox,
  EmptyState,
  Field,
  Meter,
  MetricTile,
  MetricTiles,
  Row,
  SegmentedControl,
  Select,
  Stack,
  StackedArea,
  StackedBars,
  Swatch,
  SwatchLegend,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {Fragment, type JSX} from 'react';
import {
  followed,
  groupedBy,
  groupedWithin,
  TOKEN_KINDS,
  USAGE_CHARTS,
  USAGE_GROUPS,
  USAGE_METRICS,
  type UsageContext,
  type UsageControls,
  type UsageRecord,
  type UsageRowView,
  unfolded,
  usageView,
  withKind,
} from '../../../../state/index.js';

export interface UsageViewProps {
  readonly emptyLabelsOnly?: boolean;
  readonly records: readonly UsageRecord[];
  readonly context: UsageContext;
  readonly controls: UsageControls;
  readonly change: (controls: UsageControls) => void;
}

function Name(props: {readonly row: UsageRowView}): JSX.Element {
  return (
    <Row gap="space-2" align="center">
      {props.row.swatch === null ? null : <Swatch series={props.row.swatch} />}
      {props.row.label.map((part, at) => (
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
  );
}

function columns(results: boolean): readonly TableColumn<UsageRowView>[] {
  return [
    {key: 'group', header: 'group', lead: true, cell: row => <Name row={row} />},
    ...(results
      ? [
          {
            key: 'result',
            header: 'result',
            align: 'end' as const,
            mono: true,
            cell: (row: UsageRowView) =>
              row.result === null ? null : (
                <Text variant="code" tone={row.result.tone === 'err' ? 'err' : 'muted'}>
                  {row.result.text}
                </Text>
              ),
          },
        ]
      : []),
    {key: 'turns', header: 'turns', cell: row => row.turns, mono: true, align: 'end'},
    {key: 'input', header: 'input', cell: row => row.input, mono: true, align: 'end'},
    {
      key: 'cached',
      header: 'cached',
      cell: row => row.cached,
      mono: true,
      align: 'end',
      wide: true,
    },
    {key: 'output', header: 'output', cell: row => row.output, mono: true, align: 'end'},
    {
      key: 'reasoning',
      header: 'reasoning',
      cell: row => row.reasoning,
      mono: true,
      align: 'end',
      wide: true,
    },
    {key: 'cost', header: 'cost', cell: row => row.cost, mono: true, align: 'end'},
    {
      key: 'share',
      header: 'share',
      align: 'end',
      mono: true,
      cell: row =>
        row.share === '' ? null : (
          <Row gap="space-3" align="center" justify="end">
            <Text variant="code" tone="muted">
              {row.share}
            </Text>
            <Meter
              label={`${row.label.map(part => part.text).join(' ')}: ${row.share}`}
              value={row.shareValue}
              inline
              {...(row.swatch === null ? {tone: 'muted' as const} : {series: row.swatch})}
            />
          </Row>
        ),
    },
  ];
}

function Controls(props: {
  readonly controls: UsageControls;
  readonly change: (controls: UsageControls) => void;
}): JSX.Element {
  const {controls, change} = props;
  const within = [
    {value: 'none' as const, label: 'Nothing'},
    ...USAGE_GROUPS.filter(group => group.value !== controls.by),
  ];
  return (
    <Row gap="space-5" align="center" wrap>
      <Row gap="space-3" align="center">
        <Text variant="small" tone="muted">
          Group by
        </Text>
        <SegmentedControl
          label="Group by"
          size="sm"
          options={USAGE_GROUPS}
          value={controls.by}
          change={by => change(groupedBy(controls, by))}
        />
      </Row>
      <Row gap="space-3" align="center">
        <Text variant="small" tone="muted">
          Then by
        </Text>
        <Field label="Then by" hideLabel>
          <Select
            size="sm"
            value={controls.within ?? 'none'}
            options={within}
            change={value => change(groupedWithin(controls, value === 'none' ? null : value))}
          />
        </Field>
      </Row>
      <Row gap="space-3" align="center">
        <Text variant="small" tone="muted">
          Show
        </Text>
        <SegmentedControl
          label="Count"
          size="sm"
          options={USAGE_METRICS}
          value={controls.metric}
          change={metric => change({...controls, metric})}
        />
      </Row>
      {controls.metric === 'tokens' ? (
        <Row gap="space-4" align="center" wrap>
          {TOKEN_KINDS.map(kind => (
            <Checkbox
              key={kind.value}
              label={kind.label}
              checked={controls.kinds.includes(kind.value)}
              change={() => change(withKind(controls, kind.value))}
            />
          ))}
        </Row>
      ) : null}
      <Row gap="space-3" align="center">
        <Text variant="small" tone="muted">
          Chart
        </Text>
        <SegmentedControl
          label="Chart"
          size="sm"
          options={USAGE_CHARTS}
          value={controls.chart}
          change={chart => change({...controls, chart})}
        />
      </Row>
    </Row>
  );
}

export function UsageView(props: UsageViewProps): JSX.Element {
  const {controls, change} = props;
  const view = usageView(props.records, controls, props.context);
  if (view.empty) {
    return (
      <EmptyState
        title="No usage yet"
        body={props.emptyLabelsOnly ? '' : "Each agent turn's tokens and cost show here."}
      />
    );
  }
  return (
    <Stack gap="space-4">
      <MetricTiles label="Totals" count={4}>
        {view.tiles.map(tile => (
          <MetricTile key={tile.name} name={tile.name} figure={tile.figure} detail={tile.detail} />
        ))}
      </MetricTiles>
      <Controls controls={controls} change={change} />
      {view.span === null ? null : controls.chart === 'slice' ? (
        <StackedBars
          label="Usage over time"
          unit={view.unit}
          from={view.span.from}
          to={view.span.to}
          bars={view.slices.map(slice => ({
            id: slice.id,
            title: slice.title,
            total: slice.total,
            segments: slice.segments,
          }))}
        />
      ) : (
        <StackedArea
          label="Usage, running total"
          unit={view.unit}
          from={view.span.from}
          to={view.span.to}
          titles={view.slices.map(slice => slice.title)}
          layers={view.layers}
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
      <Table
        label="Usage by group"
        columns={columns(view.results)}
        rows={view.rows}
        rowKey={row => row.id}
        rowKind={row => (row.kind === 'group' ? undefined : row.kind)}
        {...(controls.highlight === null ? {} : {selected: controls.highlight})}
        select={id => change(followed(controls, id))}
        fold={row => {
          if (controls.within === null) return 'none';
          if (row.open === null) return 'none';
          return {
            open: row.open,
            toggle: () => change(unfolded(controls, row.id)),
            label: `${row.open ? 'Collapse' : 'Expand'} ${row.label.map(part => part.text).join(' ')}`,
          };
        }}
      />
    </Stack>
  );
}
