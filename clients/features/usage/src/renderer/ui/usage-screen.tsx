/**
 * The Usage page: what the hosts spent, cut by time, provider, model and run. A header of the title,
 * a status word and the controls (tab, period, source, machines, Refresh); under it the hero figure,
 * the chips that say what the figure leaves out, the providers and the chart, the totals and their
 * shares, and the tables by model, by hour or day, by run and by agent. A model row opens its detail;
 * its price opens the dialog that sets it. Labels, figures and status words only, and every state
 * (loading, unsupported, unavailable, empty) starts at the page's left edge like the content does.
 *
 * Render is a function of the store's state; nothing here computes a figure.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  AreaChart,
  Button,
  Checkbox,
  Chip,
  Dialog,
  Field,
  LoadingBar,
  Meter,
  Page,
  Row,
  SegmentedControl,
  Select,
  type SelectOption,
  ShareBar,
  type SharePart,
  Stack,
  Swatch,
  Table,
  type TableColumn,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AgentRow,
  axisDollars,
  type DialogState,
  dollars,
  type ModelDetail,
  type ModelRow,
  type Part,
  type PricesView,
  type ProviderRow,
  type RunRow,
  type TimeRow,
  tokens,
  USAGE_PERIODS,
  type UsageActions,
  type UsageScreenState,
  type UsageSourceChoice,
  type UsageTab,
  type UsageView,
} from '../../state/index.js';

/** A run of a project on a machine, as a row of the page names it. */
export interface SpendRunRef {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
}

/** What the page calls a run: its title as every screen words it, and when it began if known. */
export interface SpendRunLabel {
  readonly title: string;
  readonly began: string | null;
}

export interface UsageScreenProps {
  readonly state: UsageScreenState;
  readonly actions: UsageActions;
  /** What a run's row is called; the shell owns run titles. */
  readonly label: (run: SpendRunRef) => SpendRunLabel;
  /** Open the run's Usage face. */
  readonly openRun: (run: SpendRunRef) => void;
}

const TABS: readonly {readonly value: UsageTab; readonly label: string}[] = [
  {value: 'cost', label: 'Cost'},
  {value: 'tokens', label: 'Tokens'},
];

const SOURCES: readonly SelectOption<UsageSourceChoice>[] = [
  {value: 'all', label: 'All sources'},
  {value: 'run', label: 'VibeSys runs'},
  {value: 'outside', label: 'Outside runs'},
];

const NO_MAP = '';
/** The hero figure's id, which an automated check names it by. */
const HERO_ID = 'usage-hero';

function Section(props: {readonly title: string; readonly children: JSX.Element}): JSX.Element {
  return (
    <Stack gap="space-3">
      <Text as="h2" variant="section">
        {props.title}
      </Text>
      {props.children}
    </Stack>
  );
}

/** A state that has nothing to draw, set at the page's left edge beside what it offers. */
function Notice(props: {readonly text: string; readonly action?: JSX.Element}): JSX.Element {
  return (
    <Row gap="space-3" align="center">
      <Text variant="heading" tone="muted">
        {props.text}
      </Text>
      {props.action ?? null}
    </Row>
  );
}

function parts(rows: readonly Part[]): readonly SharePart[] {
  return rows.map((each, at) => ({
    key: each.key,
    label: each.label,
    value: each.value,
    figure: each.figure,
    series: at,
  }));
}

function Providers(props: {
  readonly rows: readonly ProviderRow[];
  readonly toggle: (provider: string) => void;
}): JSX.Element {
  const columns: readonly TableColumn<ProviderRow>[] = [
    {
      key: 'provider',
      header: 'Provider',
      lead: true,
      cell: row => (
        <Row gap="space-2" align="center">
          <Swatch series={row.series} />
          <Checkbox
            label={row.name}
            checked={!row.hidden}
            change={() => props.toggle(row.provider)}
          />
        </Row>
      ),
    },
    {
      key: 'share',
      header: 'Share',
      wide: true,
      cell: row => <Meter label={`${row.name}: ${row.figure}`} value={row.share} inline />,
    },
    {key: 'figure', header: 'Total', mono: true, align: 'end', cell: row => row.figure},
  ];
  return (
    <Table
      label="Providers"
      columns={columns}
      rows={props.rows}
      rowKey={row => row.provider}
      middle
    />
  );
}

function modelColumns(
  tab: UsageTab,
  setPrice: (key: string) => void,
): readonly TableColumn<ModelRow>[] {
  return [
    {
      key: 'model',
      header: 'Model',
      lead: true,
      cell: row => (
        <Stack gap="space-0">
          <span title={row.id}>
            <Text variant="body-strong">{row.name}</Text>
          </span>
          <Text variant="small" tone="muted">
            {row.providerName}
          </Text>
        </Stack>
      ),
    },
    {
      key: 'figure',
      header: tab === 'cost' ? 'Cost' : 'Tokens',
      mono: true,
      align: 'end',
      cell: row => (
        <Row gap="space-2" align="center" justify="end">
          {row.unpricedTokens > 0 && row.cost !== 'Unpriced' ? (
            <Chip label="Part unpriced" tone="warn" />
          ) : null}
          {row.overridden ? <Chip label="Set" /> : null}
          {tab === 'cost' ? row.cost : row.tokensFigure}
        </Row>
      ),
    },
    {
      key: 'share',
      header: 'Share',
      wide: true,
      cell: row => (
        <Meter
          label={`${row.name}: ${tab === 'cost' ? row.cost : row.tokensFigure}`}
          value={row.share}
          inline
        />
      ),
    },
    {key: 'turns', header: 'Turns', mono: true, align: 'end', wide: true, cell: row => row.turns},
    {
      key: 'per-million',
      header: 'Per 1M tokens',
      mono: true,
      align: 'end',
      wide: true,
      cell: row => row.perMillion ?? '',
    },
    {
      key: 'cache',
      header: 'Cache hit',
      mono: true,
      align: 'end',
      wide: true,
      cell: row => (row.cacheHit === null ? '' : `${Math.round(row.cacheHit * 100)}%`),
    },
    {
      key: 'price',
      header: 'Price',
      control: true,
      cell: row => (
        <Button
          text="Price"
          variant="secondary"
          size="sm"
          press={() => setPrice(row.key)}
          hint={`Set the price of ${row.name}`}
        />
      ),
    },
  ];
}

const TIME_COLUMNS: readonly TableColumn<TimeRow>[] = [
  {key: 'label', header: 'Time', lead: true, cell: row => row.label},
  {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: row => row.cost},
  {key: 'tokens', header: 'Tokens', mono: true, align: 'end', cell: row => row.tokens},
  {key: 'turns', header: 'Turns', mono: true, align: 'end', wide: true, cell: row => row.turns},
  {key: 'runs', header: 'Runs', mono: true, align: 'end', wide: true, cell: row => row.runs},
  {
    key: 'sessions',
    header: 'Sessions',
    mono: true,
    align: 'end',
    wide: true,
    cell: row => row.sessions,
  },
];

function runColumns(label: (run: SpendRunRef) => SpendRunLabel): readonly TableColumn<RunRow>[] {
  return [
    {
      key: 'run',
      header: 'Run',
      lead: true,
      cell: row => {
        const {title, began} = label(row);
        return (
          <Stack gap="space-0">
            <Text variant="body-strong">{title}</Text>
            {began === null ? null : (
              <Text variant="small" tone="muted">
                {began}
              </Text>
            )}
          </Stack>
        );
      },
    },
    {
      key: 'share',
      header: 'Share',
      wide: true,
      cell: row => <Meter label={`${label(row).title}: ${row.cost}`} value={row.share} inline />,
    },
    {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: row => row.cost},
  ];
}

const AGENT_COLUMNS: readonly TableColumn<AgentRow>[] = [
  {
    key: 'role',
    header: 'Role',
    lead: true,
    cell: row => <Text variant="body-strong">{row.role}</Text>,
  },
  {
    key: 'model',
    header: 'Model',
    cell: row => (
      <span title={row.model}>
        <Text variant="body">{row.modelName}</Text>
      </span>
    ),
  },
  {
    key: 'share',
    header: 'Share',
    wide: true,
    cell: row => <Meter label={`${row.role}: ${row.cost}`} value={row.share} inline />,
  },
  {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: row => row.cost},
];

function Tile(props: {
  readonly name: string;
  readonly figure: string;
  readonly detail?: string | null;
}): JSX.Element {
  return (
    <Stack gap="space-0">
      <Text variant="caption" tone="muted">
        {props.name}
      </Text>
      <Text variant="heading">{props.figure}</Text>
      {props.detail === undefined || props.detail === null ? null : (
        <Text variant="small" tone="muted">
          {props.detail}
        </Text>
      )}
    </Stack>
  );
}

function Hero(props: {readonly view: UsageView}): JSX.Element {
  const {view} = props;
  return (
    <Stack gap="space-3">
      <Stack gap="space-1">
        <Text id={HERO_ID} variant="metric">
          {view.hero.figure}
        </Text>
        <Text variant="small" tone="muted">
          {view.hero.caption}
        </Text>
      </Stack>
      {view.chips.length === 0 ? null : (
        <Row gap="space-2" wrap>
          {view.chips.map(chip => (
            <Chip
              key={chip.key}
              label={chip.label}
              tone={chip.tone === 'warn' ? 'warn' : 'neutral'}
              {...(chip.hint === null ? {} : {hint: chip.hint})}
            />
          ))}
        </Row>
      )}
    </Stack>
  );
}

/** The chart, the totals and their shares. */
function Overview(props: {readonly view: UsageView; readonly tab: UsageTab}): JSX.Element {
  const {view, tab} = props;
  return (
    <>
      <AreaChart
        label="Usage over time"
        unit={view.chart.unit}
        labels={view.chart.labels}
        layers={view.chart.layers.map(layer => ({
          key: layer.provider,
          name: layer.name,
          series: layer.series,
          values: layer.values,
        }))}
        format={tab === 'cost' ? dollars : tokens}
        axis={tab === 'cost' ? axisDollars : tokens}
        stacked
      />
      <Row gap="space-6" align="start" wrap>
        {view.tiles.map(tile => (
          <Tile key={tile.name} name={tile.name} figure={tile.figure} detail={tile.detail} />
        ))}
        {tab === 'tokens' ? null : (
          <>
            <Tile
              name="Per new best"
              figure={view.perNewBest ?? '—'}
              detail={`${view.newBests} new bests`}
            />
            <Tile name="Per hour now" figure={view.perHourNow} />
          </>
        )}
      </Row>
      {tab === 'cost' ? (
        <Stack gap="space-3">
          <ShareBar label="Cost by kind" parts={parts(view.costByKind)} />
          <ShareBar label="Cost by speed" parts={parts(view.bySpeed)} />
        </Stack>
      ) : (
        <ShareBar label="Tokens by kind" parts={parts(view.tokensByKind)} />
      )}
    </>
  );
}

/** The costliest runs, each opening that run, and what the rest of the runs and the person's own use cost. */
function RunsSection(props: {
  readonly view: UsageView;
  readonly label: (run: SpendRunRef) => SpendRunLabel;
  readonly openRun: (run: SpendRunRef) => void;
}): JSX.Element | null {
  const {view, label, openRun} = props;
  if (view.runs.length === 0 && view.runsOther === null && view.outside === null) return null;
  const runs = new Map(view.runs.map(row => [row.key, row]));
  return (
    <Section title="Runs">
      <Stack gap="space-2">
        {view.runs.length === 0 ? null : (
          <Table
            label="Cost by run"
            columns={runColumns(label)}
            rows={view.runs}
            rowKey={row => row.key}
            select={key => {
              const run = runs.get(key);
              if (run !== undefined) openRun(run);
            }}
          />
        )}
        {view.runsOther === null ? null : (
          <Text variant="small" tone="muted">{`Other runs ${view.runsOther}`}</Text>
        )}
        {view.outside === null ? null : (
          <Text variant="small" tone="muted">{`Outside runs ${view.outside}`}</Text>
        )}
      </Stack>
    </Section>
  );
}

function Figures(props: UsageScreenProps & {readonly view: UsageView}): JSX.Element {
  const {view, state, actions, openRun, label} = props;
  // Nothing used: say so once. A figure of zero and a count of sessions would say it again.
  if (view.empty) return <Notice text="No usage" />;
  return (
    <Stack gap="space-5">
      <Hero view={view} />
      {view.providers.length === 0 ? null : (
        <Section title="Providers">
          <Providers rows={view.providers} toggle={actions.provider} />
        </Section>
      )}
      <Overview view={view} tab={state.tab} />
      <Section title="Models">
        <Table
          label="Models"
          middle
          columns={modelColumns(state.tab, actions.setPrice)}
          rows={view.models}
          rowKey={row => row.key}
          select={actions.model}
        />
      </Section>
      <Section title="Time">
        <Table label="Time" columns={TIME_COLUMNS} rows={view.times} rowKey={row => row.key} />
      </Section>
      <RunsSection view={view} label={label} openRun={openRun} />
      {view.agents.length === 0 ? null : (
        <Section title="Agents">
          <Table
            label="Cost by agent"
            columns={AGENT_COLUMNS}
            rows={view.agents}
            rowKey={row => row.key}
          />
        </Section>
      )}
    </Stack>
  );
}

function Body(props: UsageScreenProps): JSX.Element {
  const {state, actions} = props;
  if (state.phase === 'unsupported') return <Notice text="Update required" />;
  if (state.phase === 'failed') {
    return (
      <Notice
        text="Unavailable"
        action={
          <Button
            text="Retry"
            variant="secondary"
            size="sm"
            press={actions.refresh}
            {...(state.problem === null ? {} : {hint: state.problem})}
          />
        }
      />
    );
  }
  if (state.view === null) {
    return (
      <Stack gap="space-3">
        <LoadingBar label="Reading usage" />
        <Notice text="Loading" />
      </Stack>
    );
  }
  return <Figures {...props} view={state.view} />;
}

/** What a model cost and how it is priced: its figures, its trend, its split, and its prices. */
function ModelFigures(props: {readonly detail: ModelDetail; readonly tab: UsageTab}): JSX.Element {
  const {detail, tab} = props;
  return (
    <Stack gap="space-4">
      <Row gap="space-6" wrap>
        <Tile name="Cost" figure={detail.cost} />
        <Tile name="Tokens" figure={detail.tokens} />
        <Tile name="Turns" figure={`${detail.turns}`} />
        {detail.perMillion === null ? null : (
          <Tile name="Per 1M tokens" figure={detail.perMillion} />
        )}
        {detail.cacheHit === null ? null : <Tile name="Cache hit" figure={detail.cacheHit} />}
      </Row>
      {detail.unpriced === null ? null : <Chip label={detail.unpriced} tone="warn" />}
      {detail.priced || tab === 'tokens' ? (
        <AreaChart
          label={`${detail.name} over time`}
          unit={tab === 'cost' ? 'US dollars' : 'tokens'}
          labels={detail.trendLabels}
          layers={[{key: detail.key, name: detail.name, series: 0, values: detail.trend}]}
          format={tab === 'cost' ? dollars : tokens}
          axis={tab === 'cost' ? axisDollars : tokens}
        />
      ) : null}
      {detail.priced ? <ShareBar label="Cost by kind" parts={parts(detail.costParts)} /> : null}
      <ShareBar label="Tokens by kind" parts={parts(detail.tokenParts)} />
      <Table
        label="Prices"
        columns={[
          {key: 'label', header: 'Price', lead: true, cell: row => row.label},
          {
            key: 'text',
            header: 'Per 1M tokens',
            mono: true,
            align: 'end',
            cell: row => row.text,
          },
        ]}
        rows={detail.prices}
        rowKey={row => row.field}
      />
    </Stack>
  );
}

function ModelDialog(props: {
  readonly detail: ModelDetail | null;
  readonly tab: UsageTab;
  readonly actions: UsageActions;
}): JSX.Element {
  const {detail, tab, actions} = props;
  return (
    <Dialog
      open
      title={detail?.name ?? 'Model'}
      dismiss={actions.close}
      actions={
        <Row gap="space-2">
          {detail === null ? null : (
            <Button
              text="Set price"
              variant="secondary"
              press={() => actions.setPrice(detail.key)}
            />
          )}
          <Button text="Close" press={actions.close} />
        </Row>
      }
    >
      {detail === null ? <Notice text="No usage" /> : <ModelFigures detail={detail} tab={tab} />}
    </Dialog>
  );
}

function PricesDialog(props: {
  readonly prices: PricesView | null;
  readonly actions: UsageActions;
}): JSX.Element {
  const {prices, actions} = props;
  if (prices === null) {
    return (
      <Dialog
        open
        title="Price"
        dismiss={actions.close}
        actions={<Button text="Close" press={actions.close} />}
      >
        <Notice text="Unavailable" />
      </Dialog>
    );
  }
  const {draft} = prices;
  const targets: readonly SelectOption<string>[] = [
    {value: NO_MAP, label: 'None'},
    ...prices.targets.map(target => ({value: target, label: target})),
  ];
  return (
    <Dialog
      open
      title={prices.title}
      dismiss={actions.close}
      actions={
        <Row gap="space-2">
          <Button text="Close" variant="secondary" press={actions.close} />
          <Button
            text="Save"
            press={actions.save}
            {...(prices.canSave
              ? {}
              : {disabledReason: prices.problem ?? 'Nothing to save', quietReason: true})}
          />
        </Row>
      }
    >
      <Stack gap="space-4">
        <Checkbox
          label="Automatic"
          checked={draft.automatic}
          change={automatic => actions.edit({...draft, automatic})}
        />
        {draft.automatic ? null : (
          <Stack gap="space-3">
            {prices.fields.map(field => (
              <Field
                key={field.field}
                label={`${field.label} per 1M tokens`}
                {...(field.error ? {error: 'Invalid'} : {})}
              >
                <TextInput
                  mono
                  value={field.value}
                  {...(field.hint === null ? {} : {placeholder: field.hint})}
                  change={value =>
                    actions.edit({...draft, rate: {...draft.rate, [field.field]: value}})
                  }
                />
              </Field>
            ))}
            <Field label="Map to">
              <Select
                value={draft.mapTo}
                options={targets}
                change={mapTo => actions.edit({...draft, mapTo})}
              />
            </Field>
          </Stack>
        )}
        <Stack gap="space-2">
          {prices.destinations.map(destination => (
            <Row key={destination.host} gap="space-3" align="center" justify="between">
              <Checkbox
                label={destination.label}
                checked={destination.on}
                change={() => actions.applyTo(destination.host)}
              />
              <Row gap="space-2" align="center">
                {destination.result === null ? null : (
                  <Chip
                    label={destination.result}
                    tone={destination.result === 'Saved' ? 'ok' : 'warn'}
                  />
                )}
                {destination.retry ? (
                  <Button text="Retry" variant="secondary" size="sm" press={actions.save} />
                ) : null}
              </Row>
            </Row>
          ))}
        </Stack>
      </Stack>
    </Dialog>
  );
}

function Dialogs(props: {
  readonly dialog: DialogState;
  readonly tab: UsageTab;
  readonly actions: UsageActions;
}): JSX.Element | null {
  const {dialog, tab, actions} = props;
  if (dialog === null) return null;
  if (dialog.kind === 'model')
    return <ModelDialog detail={dialog.detail} tab={tab} actions={actions} />;
  return <PricesDialog prices={dialog.prices} actions={actions} />;
}

/** The page's header: the title, what the page is doing, the controls, and the machines to read. */
function Header(props: {
  readonly state: UsageScreenState;
  readonly actions: UsageActions;
}): JSX.Element {
  const {state, actions} = props;
  const {hosts} = state;
  return (
    <Stack gap="space-3">
      <Row justify="between" align="center" wrap>
        <Row gap="space-3" align="baseline">
          <Text as="h1" variant="title">
            Usage
          </Text>
          {state.status === null ? null : (
            <Text variant="small" tone="muted">
              {state.status}
            </Text>
          )}
        </Row>
        <Row gap="space-3" align="center" wrap>
          <SegmentedControl
            label="Measure"
            size="sm"
            options={TABS}
            value={state.tab}
            change={actions.tab}
          />
          <Field label="Period" hideLabel>
            <Select
              size="sm"
              value={state.period}
              options={USAGE_PERIODS}
              change={actions.period}
            />
          </Field>
          <Field label="Source" hideLabel>
            <Select size="sm" value={state.source} options={SOURCES} change={actions.source} />
          </Field>
          <Button text="Refresh" variant="secondary" size="sm" press={actions.refresh} />
        </Row>
      </Row>
      {hosts.length < 2 ? null : (
        <Row gap="space-4" wrap>
          {hosts.map(host => (
            <span key={host.host} title={host.status}>
              <Checkbox
                label={host.label}
                checked={host.on}
                change={() => actions.host(host.host)}
              />
            </span>
          ))}
        </Row>
      )}
    </Stack>
  );
}

export function UsageScreen(props: UsageScreenProps): JSX.Element {
  const {state, actions} = props;
  return (
    <Page width="wide" header={<Header state={state} actions={actions} />}>
      <Body {...props} />
      <Dialogs dialog={state.dialog} tab={state.tab} actions={actions} />
    </Page>
  );
}
