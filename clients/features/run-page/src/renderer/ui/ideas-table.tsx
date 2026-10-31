/**
 * The ideas of the run as a table: what each built on, its status and cause, what it did to each
 * objective, its multiple of the input, its share of the gain and its agent time, under the status
 * chips and the search. Every figure is the state's (`ideasTable`); this draws them. A row pressed
 * is the idea the charts single out, and the rows that built it carry a bar.
 */
import {
  CellInline,
  CellLines,
  CellLink,
  CellMark,
  Chip,
  ColumnList,
  DotLegend,
  type LegendItem,
  type ListColumn,
  markdownToPlain,
  Row,
  SearchBox,
  Stack,
  StatusDot,
  type StatusTone,
  Text,
  type TextTone,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {NO_CLEAR_CHANGE} from '../../state/headline.js';
import {
  DEFAULT_SORT,
  type IdeaStatus,
  type IdeasFilter,
  type IdeasSort,
  type IdeasTable as IdeasTableView,
  type IdeaTableRow,
  ideasTable,
  type ObjectiveCell,
  type RunPageAction,
  runningWords,
  type TableColumn,
} from '../../state/index.js';
import {formatValue, ratio} from '../../state/span-reading.js';
import type {ViewBase} from './views/props.js';

const STATUS_TONES: Readonly<Record<IdeaStatus, StatusTone>> = {
  running: 'live',
  faster: 'ok',
  'no-gain': 'neutral',
  'not-measured': 'warn',
  dropped: 'neutral',
  failed: 'err',
};

const CHANGE_TONES: Readonly<Record<NonNullable<ObjectiveCell['change']>, TextTone>> = {
  faster: 'ok',
  slower: 'err',
  same: 'muted',
};

/** What an empty cell says. */
const NONE = '–';

/** A reading's multiple as a cell says it: the multiple, in its tone, or `No clear change`. */
function Multiple(props: {readonly cell: ObjectiveCell}): JSX.Element | null {
  const {multiple, change} = props.cell;
  if (multiple === null || change === null) return null;
  return (
    <Text variant="code" tone={CHANGE_TONES[change]}>
      {change === 'same' ? NO_CLEAR_CHANGE : ratio(multiple)}
    </Text>
  );
}

/** The cell of one objective: a dot where the reading set a best, the value, the multiple. */
function ObjectiveFigure(props: {
  readonly cell: ObjectiveCell;
  readonly column: TableColumn;
  readonly form: IdeasTableView['form'];
}): JSX.Element {
  const {cell, column, form} = props;
  if (cell.value === null) return <Text tone="muted">{NONE}</Text>;
  const value = formatValue(cell.value);
  const exact = `${value}${column.unit === '' ? '' : ` ${column.unit}`}`;
  // The unit is the column's header; the cell drops it when the list is narrow (`data-optional`).
  const alone = column.unit === '' ? null : <span data-optional>{` ${column.unit}`}</span>;
  return (
    <span title={exact}>
      <CellInline>
        <CellMark on={cell.setBest} />
        {form === 'multiple' ? null : (
          // Beside a multiple the value gives way when the list is narrow; alone it keeps its place.
          <span data-optional={form === 'value-multiple' || undefined}>
            <Text variant="code">
              {value}
              {form === 'value' ? alone : null}
            </Text>
          </span>
        )}
        {form === 'value' ? null : <Multiple cell={cell} />}
      </CellInline>
    </span>
  );
}

function IdeaName(props: {readonly row: IdeaTableRow}): JSX.Element {
  const {row} = props;
  return (
    <CellInline>
      <Text variant="code">{markdownToPlain(row.title)}</Text>
      {row.adopted ? <Chip label="Adopted" tone="ok" /> : null}
      {row.best ? <Chip label="Best" tone="ok" /> : null}
    </CellInline>
  );
}

function sameSort(a: IdeasSort, b: IdeasSort): boolean {
  if (a.by === 'objective' && b.by === 'objective') return a.index === b.index;
  return a.by === b.by;
}

/** A press on the idea a row was built on opens that idea; the input has none to open. */
function openParent(base: ViewBase, row: IdeaTableRow): boolean {
  if (row.builtOn.parent === null) return false;
  base.zoom({type: 'open-idea', idea: row.builtOn.parent});
  return true;
}

type Column = ListColumn<IdeaTableRow>;
type Sorting = NonNullable<Column['sort']>;

function quiet(text: string): JSX.Element {
  return <Text tone="muted">{text}</Text>;
}

function objectiveColumns(
  table: IdeasTableView,
  sorted: (by: IdeasSort) => Sorting,
): readonly Column[] {
  return table.columns.map(
    (column, at): Column => ({
      key: `objective-${column.index}`,
      header: column.title,
      size: table.form === 'multiple' ? 'short' : 'wide',
      narrow: 'short',
      hint: row => {
        const cell = row.cells[at];
        return cell?.value == null ? undefined : `${formatValue(cell.value)} ${column.unit}`.trim();
      },
      ...(at > 0 ? {optional: true} : {}),
      sort: sorted({by: 'objective', index: column.index}),
      cell: row => {
        const cell = row.cells[at];
        return cell === undefined ? (
          quiet(NONE)
        ) : (
          <ObjectiveFigure cell={cell} column={column} form={table.form} />
        );
      },
    }),
  );
}

/** The columns of a run with one objective: the multiple of the input, each comparison, the share. */
function singleColumns(table: IdeasTableView): readonly Column[] {
  const vsInput: Column = {
    key: 'vs-input',
    header: 'vs input',
    size: 'short',
    optional: true,
    cell: row => {
      const cell = row.cells[0];
      return cell === undefined || cell.multiple === null ? quiet(NONE) : <Multiple cell={cell} />;
    },
  };
  const comparisons = table.baselines.map(
    (label, at): Column => ({
      key: `baseline-${at}`,
      header: label,
      size: 'short',
      optional: true,
      cell: row => <Text variant="code">{row.baselines[at] ?? NONE}</Text>,
    }),
  );
  const share: Column = {
    key: 'share',
    header: 'Share of gain',
    size: 'wide',
    optional: true,
    cell: row =>
      row.share === null ? (
        quiet(NONE)
      ) : (
        <Text variant="code">{`+${formatValue(row.share.gain)} ${row.share.percent}%`}</Text>
      ),
  };
  return [vsInput, ...comparisons, ...(table.shares ? [share] : [])];
}

function leadColumns(base: ViewBase): readonly Column[] {
  return [
    {
      key: 'idea',
      header: 'Idea',
      size: 'fill',
      wrap: true,
      hint: row => markdownToPlain(row.title),
      cell: row => <IdeaName row={row} />,
    },
    {
      key: 'built-on',
      header: 'Built on',
      size: 'wide',
      optional: true,
      hint: row => markdownToPlain(row.builtOn.label),
      act: row => openParent(base, row),
      cell: row =>
        row.builtOn.parent === null ? (
          quiet(row.builtOn.label)
        ) : (
          <CellLink label={markdownToPlain(row.builtOn.label)} />
        ),
    },
    {
      key: 'status',
      header: 'Status',
      size: 'wide',
      narrow: 'medium',
      wrap: true,
      hint: row => [row.status.word, row.status.cause].filter(part => part !== null).join(' · '),
      cell: row => (
        <CellLines
          main={
            <CellInline nowrap>
              <StatusDot tone={STATUS_TONES[row.status.status]} label={row.status.word} />
              <Text>{row.status.word}</Text>
            </CellInline>
          }
          note={row.status.cause}
        />
      ),
    },
  ];
}

function columnsOf(
  table: IdeasTableView,
  base: ViewBase,
  setSort: (sort: IdeasSort | null) => void,
): readonly Column[] {
  const sorted = (by: IdeasSort): Sorting => ({
    marked: sameSort(table.sort, by),
    press: () => setSort(sameSort(by, DEFAULT_SORT) ? null : by),
  });
  return [
    ...leadColumns(base),
    ...objectiveColumns(table, sorted),
    ...(table.form === 'value' ? singleColumns(table) : []),
    {
      key: 'agent-time',
      header: 'Agent time',
      size: 'short',
      align: 'end',
      optional: true,
      sort: sorted({by: 'agent-time'}),
      cell: row => <Text variant="code">{runningWords(row.agentSeconds)}</Text>,
    },
  ];
}

function Chips(props: {
  readonly table: IdeasTableView;
  readonly filter: IdeasFilter;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {table, filter, dispatch} = props;
  const items: LegendItem[] = table.chips.map(chip => ({
    tone: chip.status === null ? 'neutral' : STATUS_TONES[chip.status],
    label: `${chip.word} ${chip.count}`,
    on: filter.status === chip.status,
    // The chip of the status the list already shows has nothing to change.
    ...(chip.status === null ? {disabled: filter.status === null} : {}),
    toggle: () =>
      dispatch({
        type: 'ideas-filter',
        filter: {...filter, status: filter.status === chip.status ? null : chip.status},
      }),
  }));
  return <DotLegend items={items} />;
}

export function IdeasTable(props: {
  readonly base: ViewBase;
  readonly filter: IdeasFilter;
  readonly sort: IdeasSort | null;
  readonly setSort: (sort: IdeasSort | null) => void;
  readonly selected: string | null;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {base, filter, sort} = props;
  const table = ideasTable(base.model, {
    objectives: base.objectives,
    filter,
    sort,
    selected: props.selected,
    now: base.clock.now,
  });
  const shown = table.rows.length;
  return (
    <Stack gap="space-3" grow>
      <Row gap="space-3" align="center">
        <Row gap="space-3" align="baseline">
          <Text variant="heading">Ideas</Text>
          <Text tone="muted">
            {shown === table.total ? `${table.total}` : `${shown} of ${table.total}`}
          </Text>
        </Row>
        <Row gap="space-0" grow justify="end">
          <SearchBox
            label="Search ideas"
            placeholder="Search ideas"
            value={filter.query}
            change={query => props.dispatch({type: 'ideas-filter', filter: {...filter, query}})}
          />
        </Row>
      </Row>
      <Chips table={table} filter={filter} dispatch={props.dispatch} />
      {shown === 0 ? (
        <Text tone="muted">No matches</Text>
      ) : (
        <ColumnList
          label="Ideas"
          columns={columnsOf(table, base, props.setSort)}
          rows={table.rows}
          rowKey={row => row.id}
          selected={props.selected ?? undefined}
          select={id => base.zoom({type: 'open-idea', idea: id})}
          hover={id => props.dispatch({type: 'ideas-hover', idea: id})}
          marks={row => ({lineage: row.lineage, fresh: row.fresh})}
        />
      )}
    </Stack>
  );
}
