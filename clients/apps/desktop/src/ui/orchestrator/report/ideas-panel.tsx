import {
  Card,
  Chip,
  DotLegend,
  Row,
  SearchBox,
  SegmentedControl,
  Stack,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {type JSX, useRef, useState} from 'react';
import {
  type GainView,
  type IdeaRow,
  type IdeaSortKey,
  type IdeasReport,
  type IdeaWindow,
  ideasInRun,
  ideasInWindow,
  ideasMatching,
  ideasOfVerdict,
  nextSort,
  type SortDirection,
  sortIdeas,
  VERDICT_ORDER,
  type Verdict,
  verdictCounts,
} from '../../../state/index.js';
import {useDisplayClock} from '../../display-clock.js';
import {nameOf} from '../names.js';
import {formatAge, formatExact, formatGain, withUnit} from './format.js';
import {VERDICT_TONES, VERDICT_WORDS} from './verdicts.js';
import {yet} from './yet.js';

type Scope = 'run' | 'all' | 'window';
type VerdictFilter = 'any' | Verdict;

/** What a hover says of an idea's gains: every objective, with the values behind the ratio. */
function gainHint(gains: readonly GainView[], units: ReadonlyMap<string, string | null>): string {
  return gains
    .map(
      gain =>
        `${gain.metric} ${formatGain(gain.ratio)} (${withUnit(formatExact(gain.bestValue), units.get(gain.metric) ?? null)} against ${withUnit(formatExact(gain.baselineValue), units.get(gain.metric) ?? null)})`,
    )
    .join('\n');
}

/** The first objective's gain, which the table's one column shows; the rest are in the hover. */
function GainCell(props: {
  readonly gains: readonly GainView[];
  readonly units: ReadonlyMap<string, string | null>;
}): JSX.Element {
  const first = props.gains[0];
  if (first === undefined) return <Text tone="muted">—</Text>;
  return (
    <Text variant="code" title={gainHint(props.gains, props.units)}>
      {formatGain(first.ratio)}
    </Text>
  );
}

interface TableWords {
  readonly names: ReadonlyMap<string, string>;
  readonly units: ReadonlyMap<string, string | null>;
  readonly nowMs: number;
  readonly moment: (atMs: number) => string;
}

/** The columns of the ideas table: the numbers and words the host served, one to a column, each a sort. */
function ideaColumns(words: TableWords): TableColumn<IdeaRow>[] {
  const {names, units} = words;
  const agentOf = (row: IdeaRow): string => (row.agent === '' ? '' : nameOf(names, row.agent));
  return [
    {
      key: 'id',
      header: '#',
      sortable: true,
      mono: true,
      align: 'end',
      cell: r => `#${r.planNumber}`,
    },
    {
      key: 'idea',
      header: 'Idea',
      sortable: true,
      lead: true,
      clip: row => row.title,
      cell: row => row.title,
    },
    {
      key: 'workstream',
      header: 'Workstream',
      sortable: true,
      wide: true,
      clip: row => row.workstreamTitle ?? '',
      cell: row => row.workstreamTitle ?? '—',
    },
    {
      key: 'verdict',
      header: 'Verdict',
      sortable: true,
      cell: row => (
        <Chip label={VERDICT_WORDS[row.verdict]} tone={VERDICT_TONES[row.verdict]} dot />
      ),
    },
    {
      key: 'gain',
      header: 'Gain',
      sortable: true,
      mono: true,
      align: 'end',
      cell: row => <GainCell gains={row.gains} units={units} />,
    },
    {
      key: 'agent',
      header: 'Agent',
      sortable: true,
      wide: true,
      clip: agentOf,
      cell: row => (row.agent === '' ? '—' : agentOf(row)),
    },
    {
      key: 'updated',
      header: 'Updated',
      sortable: true,
      mono: true,
      align: 'end',
      cell: row =>
        row.updatedMs === null ? (
          '—'
        ) : (
          <Text variant="code" tone="muted" title={words.moment(row.updatedMs)}>
            {formatAge(words.nowMs - row.updatedMs)}
          </Text>
        ),
    },
  ];
}

const SORT_KEYS: readonly IdeaSortKey[] = [
  'id',
  'idea',
  'workstream',
  'verdict',
  'gain',
  'agent',
  'updated',
];

/** What the count of measurements that name no plan says: the baseline and the ones made without one. */
const unplannedWords = (count: number): string =>
  `${formatExact(count)} ${count === 1 ? 'measurement' : 'measurements'} had no plan`;

/** What the table says with no row to show: nothing proposed yet (and what was measured meanwhile), or no match. */
function emptyWords(ideas: number, unplanned: number, over: boolean): string {
  if (ideas > 0) return 'No ideas match';
  const none = yet('No ideas', over);
  if (unplanned > 0) return `${none} · ${unplannedWords(unplanned)}`;
  return over ? none : `${none} · they appear here as agents propose them`;
}

/** What the controls over the table have chosen: the scope, the verdict, the search and the order. */
interface Controls {
  readonly scope: Scope;
  readonly setScope: (scope: Scope) => void;
  readonly verdict: VerdictFilter;
  readonly setVerdict: (verdict: VerdictFilter) => void;
  readonly search: string;
  readonly setSearch: (search: string) => void;
  readonly sort: {readonly key: IdeaSortKey; readonly direction: SortDirection};
  readonly setSort: (sort: {readonly key: IdeaSortKey; readonly direction: SortDirection}) => void;
}

/**
 * The controls' state. A new window from the route, or a chart point asking for its idea, resets what
 * the controls hide, so the idea asked for is in the table.
 */
function useControls(window: IdeaWindow | null, reveal: number): Controls {
  const [scope, setScope] = useState<Scope>(window === null ? 'run' : 'window');
  const [verdict, setVerdict] = useState<VerdictFilter>('any');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<Controls['sort']>({key: 'updated', direction: 'descending'});
  const [shownWindow, setShownWindow] = useState(window);
  const [seen, setSeen] = useState(reveal);
  if (shownWindow !== window) {
    setShownWindow(window);
    setScope(window === null ? 'run' : 'window');
    setVerdict('any');
    setSearch('');
  }
  if (seen !== reveal) {
    setSeen(reveal);
    setScope('all');
    setVerdict('any');
    setSearch('');
  }
  return {scope, setScope, verdict, setVerdict, search, setSearch, sort, setSort};
}

/** The chips that filter by verdict, each with its count and color, and the choice of which ideas are in scope. */
function Filters(props: {
  readonly controls: Controls;
  readonly scoped: readonly IdeaRow[];
  readonly inScope: Readonly<Record<Scope, readonly IdeaRow[]>>;
  readonly hasWindow: boolean;
}): JSX.Element {
  const {controls, scoped, inScope} = props;
  const counts = verdictCounts(scoped);
  const chips = [
    {tone: 'neutral' as const, label: 'All', count: scoped.length, key: 'any' as const},
    ...VERDICT_ORDER.map(each => ({
      tone: VERDICT_TONES[each],
      label: VERDICT_WORDS[each],
      count: counts[each],
      key: each,
    })),
  ];
  const scopes = [
    {value: 'run' as const, label: `This run ${inScope.run.length}`},
    {value: 'all' as const, label: `All ideas ${inScope.all.length}`},
    ...(props.hasWindow
      ? [{value: 'window' as const, label: `Check-in ${inScope.window.length}`}]
      : []),
  ];
  return (
    <Row gap="space-4" rowGap="space-2" wrap justify="between">
      <DotLegend
        items={chips.map(chip => ({
          tone: chip.tone,
          label: chip.label,
          count: chip.count,
          on: controls.verdict === chip.key,
          disabled: chip.count === 0 && controls.verdict !== chip.key,
          toggle: on => controls.setVerdict(on ? chip.key : 'any'),
        }))}
      />
      {props.hasWindow || inScope.all.length !== inScope.run.length ? (
        <SegmentedControl
          label="Ideas shown"
          size="xs"
          options={scopes}
          value={controls.scope}
          change={controls.setScope}
        />
      ) : null}
    </Row>
  );
}

/**
 * The run's ideas as a dense table that scrolls in a box of its own, under chips that filter by
 * verdict (each with its count and the verdict's color) and a search of the words in a row. Columns:
 * number, idea, workstream, verdict, gain against the baseline, who made it and how long ago it
 * changed; every header sorts. Every figure and word is the host's: the controls only choose which
 * rows to show and their order (this run's ideas by default, all of the task's, or what a check-in
 * covers). A title or name longer than its column is cut with an ellipsis and whole on hover. Picking
 * a row lights its candidates on the charts (`select`). With no ideas the table keeps its headers and
 * says so in one row, in the same place.
 */
export function IdeasPanel(props: {
  readonly ideas: IdeasReport | undefined;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  /** What each objective is counted in, by name. */
  readonly units: ReadonlyMap<string, string | null>;
  readonly nowMs: number;
  /** The check-in span the table is narrowed to when it opens; null for the run's own ideas. */
  readonly window: IdeaWindow | null;
  /** The plan number singled out, or null. */
  readonly selected: number | null;
  readonly select: (plan: number | null) => void;
  /** A chart point asked to see its idea: a new count each time, so the table shows every row. */
  readonly reveal: number;
  /** The run has ended: no idea is still to come. */
  readonly runOver: boolean;
}): JSX.Element {
  const clock = useDisplayClock();
  const all = props.ideas?.ideas ?? [];
  const controls = useControls(props.window, props.reveal);
  const handled = useRef(props.reveal);
  // The row a chart point asked for: brought into view and focused once, when the table has it.
  const focusRow = (box: HTMLDivElement | null): void => {
    if (box === null || handled.current === props.reveal) return;
    handled.current = props.reveal;
    const row = box.querySelector<HTMLElement>('tr[aria-current="true"]');
    row?.scrollIntoView({block: 'nearest'});
    row?.focus();
  };
  const window = props.window;
  const inScope: Record<Scope, readonly IdeaRow[]> = {
    run: ideasInRun(all),
    all,
    window: window === null ? [] : ideasInWindow(all, window),
  };
  const scoped = inScope[controls.scope];
  const {verdict, search, sort} = controls;
  const rows = sortIdeas(
    ideasMatching(verdict === 'any' ? scoped : ideasOfVerdict(scoped, verdict), search),
    sort,
    agent => nameOf(props.names, agent),
  );
  const words: TableWords = {
    names: props.names,
    units: props.units,
    nowMs: props.nowMs,
    moment: clock.reportMoment,
  };
  return (
    <Card
      dense
      section="Ideas"
      action={
        <SearchBox
          label="Find an idea"
          placeholder="Find an idea"
          value={search}
          change={controls.setSearch}
        />
      }
    >
      <Stack gap="space-3">
        <Filters
          controls={controls}
          scoped={scoped}
          inScope={inScope}
          hasWindow={window !== null}
        />
        <div ref={focusRow}>
          <Table
            label="Idea list"
            dense
            middle
            scroll="down"
            rows={rows}
            rowKey={row => String(row.planNumber)}
            {...(props.selected === null ? {} : {selected: String(props.selected)})}
            select={key => {
              const plan = Number(key);
              props.select(plan === props.selected ? null : plan);
            }}
            sort={{
              key: sort.key,
              direction: sort.direction,
              change: key => {
                const pressed = SORT_KEYS.find(each => each === key);
                if (pressed !== undefined) controls.setSort(nextSort(sort, pressed));
              },
            }}
            columns={ideaColumns(words)}
            empty={emptyWords(all.length, props.ideas?.unplanned ?? 0, props.runOver)}
          />
        </div>
        {props.ideas === undefined ||
        all.length === 0 ||
        (props.ideas.unplanned === 0 && !props.ideas.truncated) ? null : (
          <Row gap="space-4" wrap>
            {props.ideas.unplanned === 0 ? null : (
              <Text variant="small" tone="muted">
                {unplannedWords(props.ideas.unplanned)}
              </Text>
            )}
            {props.ideas.truncated ? <Chip label="Some left out" tone="warn" /> : null}
          </Row>
        )}
      </Stack>
    </Card>
  );
}
