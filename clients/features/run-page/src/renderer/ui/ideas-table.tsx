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
import type {JSX, ReactNode} from 'react';
import {
  compactValue,
  endOf,
  formatValue,
  type IdeaStatus,
  type IdeasFilter,
  type IdeasSort,
  type IdeasTable as IdeasTableView,
  type IdeaTableRow,
  ideasTable,
  NO_CLEAR_CHANGE,
  type ObjectiveCell,
  type RunPageAction,
  ratio,
  runningWords,
  type TableColumn,
} from '../../state/index.js';
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

/**
 * How a multiple within noise of the input reads: the muted multiple (`ratio`), `No clear change`
 * (`words`), or the words where the cell has room and the multiple where it has not (`both`).
 */
type FlatReading = 'ratio' | 'words' | 'both';

/** A reading's multiple as a cell says it: the multiple in its tone, and a flat one as `flat` says. */
function Multiple(props: {
  readonly cell: ObjectiveCell;
  readonly flat: FlatReading;
}): JSX.Element | null {
  const {multiple, change} = props.cell;
  if (multiple === null || change === null) return null;
  const figure = (
    <Text variant="code" tone={CHANGE_TONES[change]}>
      {ratio(multiple)}
    </Text>
  );
  if (change !== 'same' || props.flat === 'ratio') return figure;
  const words = (
    <Text variant="code" tone="muted">
      {NO_CLEAR_CHANGE}
    </Text>
  );
  if (props.flat === 'words') return words;
  return (
    <>
      <span data-optional>{words}</span>
      <span data-narrow-only>{figure}</span>
    </>
  );
}

/**
 * The cell of one objective: a dot where the reading set a best, the value, the multiple. The value
 * is compact (`6.05M`), its exact figure the cell's hint. While the list is narrow the cell keeps the
 * multiple alone, for every objective: a run of one objective draws it here, where the `vs input`
 * column that states it while the list is wide has given way.
 */
function ObjectiveFigure(props: {
  readonly cell: ObjectiveCell;
  readonly column: TableColumn;
  readonly form: IdeasTableView['form'];
}): JSX.Element {
  const {cell, column, form} = props;
  if (cell.value === null) return <Text tone="muted">{NONE}</Text>;
  const unit = form === 'value' && column.unit !== '' ? ` ${column.unit}` : '';
  // A reading with no multiple (no input) keeps its value when the list is narrow.
  const gives = cell.multiple !== null;
  return (
    <CellInline>
      <CellMark on={cell.setBest} />
      {form === 'multiple' ? null : (
        <span data-optional={gives || undefined}>
          <Text variant="code">{`${compactValue(cell.value)}${unit}`}</Text>
        </span>
      )}
      {form === 'value' ? (
        <span data-narrow-only>
          <Multiple cell={cell} flat="ratio" />
        </span>
      ) : (
        <Multiple cell={cell} flat={form === 'value-multiple' ? 'both' : 'ratio'} />
      )}
    </CellInline>
  );
}

/** The idea's name with its tags, and what it is doing under them while it runs. */
function IdeaName(props: {readonly row: IdeaTableRow}): JSX.Element {
  const {row} = props;
  return (
    <CellLines
      main={
        <CellInline>
          <Text variant="code">{markdownToPlain(row.title)}</Text>
          {row.adopted ? <Chip label="Adopted" tone="ok" /> : null}
          {row.best ? <Chip label="Best" tone="ok" /> : null}
        </CellInline>
      }
      note={row.doing}
    />
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
      size: table.form === 'multiple' ? 'short' : 'medium',
      narrow: 'short',
      tight: 'compact',
      hint: row => {
        const cell = row.cells[at];
        return cell?.value == null ? undefined : `${formatValue(cell.value)} ${column.unit}`.trim();
      },
      ...(at === table.columns.length - 1 && table.more > 0
        ? {note: {label: `+${table.more}`, hint: `${table.more} more objectives`}}
        : {}),
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

/** A gain with its sign: a line that ended below its best has a negative one. */
function signed(gain: number): string {
  return gain < 0 ? `−${compactValue(-gain)}` : `+${compactValue(gain)}`;
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
      return cell === undefined || cell.multiple === null ? (
        quiet(NONE)
      ) : (
        <Multiple cell={cell} flat="words" />
      );
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
    size: 'medium',
    optional: true,
    cell: row =>
      row.share === null ? (
        quiet(NONE)
      ) : (
        <Text variant="code">{`${signed(row.share.gain)} ${row.share.percent}%`}</Text>
      ),
  };
  // A run with no input has no multiple to state: its column would read a dash on every row.
  const known = table.columns[0]?.input != null;
  return [...(known ? [vsInput] : []), ...comparisons, ...(table.shares ? [share] : [])];
}

/**
 * What the idea was built on, a link to that idea. It sits after the figures, not beside the name:
 * a press on the middle of a row picks the row, and the middle of a row is not on a link.
 */
function builtOnColumn(base: ViewBase): Column {
  return {
    key: 'built-on',
    header: 'Built on',
    size: 'shrink',
    optional: true,
    hint: row => markdownToPlain(row.builtOn.label),
    act: row => openParent(base, row),
    cell: row =>
      row.builtOn.parent === null ? (
        quiet(row.builtOn.label)
      ) : (
        <CellLink label={markdownToPlain(row.builtOn.label)} lit={row.lineage || row.selected} />
      ),
  };
}

function leadColumns(): readonly Column[] {
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
      key: 'status',
      header: 'Status',
      size: 'medium',
      tight: 'dot',
      hint: row => [row.status.word, row.status.cause].filter(part => part !== null).join(' · '),
      cell: row => (
        <CellLines
          main={
            <CellInline nowrap>
              <StatusDot tone={STATUS_TONES[row.status.status]} label={row.status.word} />
              <span data-tight-hide>
                <Text>{row.status.word}</Text>
              </span>
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
    press: () => setSort(sameSort(by, table.defaultSort) ? null : by),
  });
  return [
    ...leadColumns(),
    ...objectiveColumns(table, sorted),
    ...(table.form === 'value' ? singleColumns(table) : []),
    builtOnColumn(base),
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

/** What a row says besides its name, whole: its status, what it built on, and its figures under each objective's name. */
function describeRow(table: IdeasTableView, row: IdeaTableRow): string {
  const figures = table.columns.flatMap((column, at) => {
    const value = row.cells[at]?.value;
    return value == null
      ? []
      : [`${column.title} ${`${formatValue(value)} ${column.unit}`.trim()}`];
  });
  return [
    [row.status.word, row.status.cause].filter(part => part !== null).join(', '),
    `built on ${markdownToPlain(row.builtOn.label)}`,
    ...figures,
  ].join(', ');
}

export function IdeasTable(props: {
  readonly base: ViewBase;
  readonly filter: IdeasFilter;
  readonly sort: IdeasSort | null;
  readonly setSort: (sort: IdeasSort | null) => void;
  readonly selected: string | null;
  readonly dispatch: (action: RunPageAction) => void;
  /** What leads the row of chips and search (the face switcher), so the table starts higher. */
  readonly lead?: ReactNode;
}): JSX.Element {
  const {base, filter, sort} = props;
  const table = ideasTable(base.model, {
    position: base.position ?? endOf(base.model),
    filter,
    sort,
    selected: props.selected,
    now: base.clock.now,
  });
  const shown = table.rows.length;
  return (
    <Stack gap="space-3" grow>
      <Row gap="space-2" align="center" wrap justify="between">
        <Row gap="space-5" align="center" wrap>
          {props.lead}
          <Chips table={table} filter={filter} dispatch={props.dispatch} />
        </Row>
        <SearchBox
          label="Search ideas"
          placeholder="Search ideas"
          value={filter.query}
          change={query => props.dispatch({type: 'ideas-filter', filter: {...filter, query}})}
        />
      </Row>
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
          describe={row => describeRow(table, row)}
          crowded={table.columns.length > 1}
        />
      )}
    </Stack>
  );
}
