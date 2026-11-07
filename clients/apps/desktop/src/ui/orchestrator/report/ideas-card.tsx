import {
  Card,
  Chip,
  Row,
  SegmentedControl,
  Stack,
  type StatusTone,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {type JSX, useRef, useState} from 'react';
import {
  type GainView,
  type IdeaRow,
  type IdeasReport,
  type IdeaWindow,
  ideasInRun,
  ideasInWindow,
  ideasOfVerdict,
  type Verdict,
} from '../../../state/index.js';
import {nameOf} from '../names.js';
import {formatExact, formatGain, withUnit} from './format.js';

type Scope = 'run' | 'all' | 'window';
type VerdictFilter = 'any' | Verdict;

const VERDICT_WORDS = {
  open: 'Open',
  kept: 'Kept',
  rejected: 'Rejected',
  parked: 'Parked',
} as const satisfies Record<Verdict, string>;

const VERDICT_TONES = {
  open: 'warn',
  kept: 'ok',
  rejected: 'neutral',
  parked: 'live',
} as const satisfies Record<Verdict, StatusTone>;

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
    <Text
      variant="code"
      tone={first.ratio > 1 ? 'ok' : 'muted'}
      title={gainHint(props.gains, props.units)}
    >
      {formatGain(first.ratio)}
    </Text>
  );
}

/** What a set of ideas is counted as in a filter's label. */
const counted = (label: string, rows: readonly IdeaRow[]): string => `${label} ${rows.length}`;

/** The columns of the ideas table: the numbers and words the host served, one to a column. */
function ideaColumns(
  names: ReadonlyMap<string, string>,
  units: ReadonlyMap<string, string | null>,
): TableColumn<IdeaRow>[] {
  return [
    {
      key: 'id',
      header: 'ID',
      mono: true,
      align: 'end',
      cell: row => `#${row.planNumber}`,
    },
    {
      key: 'idea',
      header: 'Idea',
      lead: true,
      clip: row => row.title,
      cell: row => row.title,
    },
    {
      key: 'workstream',
      header: 'Workstream',
      wide: true,
      clip: row => row.workstreamTitle ?? '',
      cell: row => row.workstreamTitle ?? '—',
    },
    {
      key: 'verdict',
      header: 'Verdict',
      cell: row => <Chip label={VERDICT_WORDS[row.verdict]} tone={VERDICT_TONES[row.verdict]} />,
    },
    {
      key: 'gain',
      header: 'Gain',
      mono: true,
      align: 'end',
      cell: row => <GainCell gains={row.gains} units={units} />,
    },
    {
      key: 'candidates',
      header: 'Cand.',
      mono: true,
      align: 'end',
      cell: row => String(row.candidateJobIds.length),
    },
    {
      key: 'agent',
      header: 'Agent',
      wide: true,
      clip: row => (row.agent === '' ? '' : nameOf(names, row.agent)),
      cell: row => (row.agent === '' ? '—' : nameOf(names, row.agent)),
    },
  ];
}

/**
 * The run's ideas as a table: number, title, workstream, verdict, best gain against the baseline,
 * candidates and who made it. Every figure and word is the host's; the controls only choose which
 * rows to show (the run's own, all, or what a check-in covers, and one verdict). A title or name
 * longer than its column is cut with an ellipsis and whole on hover. Picking a row lights its
 * candidates on the charts (`select`).
 */
export function IdeasCard(props: {
  readonly ideas: IdeasReport;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  /** What each objective is counted in, by name. */
  readonly units: ReadonlyMap<string, string | null>;
  /** The check-in span the table is narrowed to when it opens; null for the run's own ideas. */
  readonly window: IdeaWindow | null;
  /** The plan number singled out, or null. */
  readonly selected: number | null;
  readonly select: (plan: number | null) => void;
  /** A chart point asked to see its idea: a new count each time, so the table shows every row. */
  readonly reveal: number;
}): JSX.Element {
  const {ideas} = props;
  const [scope, setScope] = useState<Scope>(props.window === null ? 'run' : 'window');
  const [verdict, setVerdict] = useState<VerdictFilter>('any');
  const [shownWindow, setShownWindow] = useState(props.window);
  const [seen, setSeen] = useState(props.reveal);
  // A new window from the route, or a chart point asking for its idea, resets what the controls hide.
  if (shownWindow !== props.window) {
    setShownWindow(props.window);
    setScope(props.window === null ? 'run' : 'window');
    setVerdict('any');
  }
  if (seen !== props.reveal) {
    setSeen(props.reveal);
    setScope('all');
    setVerdict('any');
  }
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
    run: ideasInRun(ideas.ideas),
    all: ideas.ideas,
    window: window === null ? [] : ideasInWindow(ideas.ideas, window),
  };
  const scoped = inScope[scope];
  const rows = verdict === 'any' ? scoped : ideasOfVerdict(scoped, verdict);
  const present = (['kept', 'rejected', 'open', 'parked'] as const).filter(
    each => each === verdict || ideasOfVerdict(scoped, each).length > 0,
  );
  const scopes = [
    {value: 'run' as const, label: counted('In run', inScope.run)},
    {value: 'all' as const, label: counted('All', inScope.all)},
    ...(window === null
      ? []
      : [{value: 'window' as const, label: counted('Check-in', inScope.window)}]),
  ];
  const verdicts = [
    {value: 'any' as const, label: counted('All verdicts', scoped)},
    ...present.map(each => ({
      value: each,
      label: counted(VERDICT_WORDS[each], ideasOfVerdict(scoped, each)),
    })),
  ];
  return (
    <Card section="Ideas">
      <Stack gap="space-4">
        <Row gap="space-4" rowGap="space-3" wrap justify="between">
          <SegmentedControl
            label="Ideas shown"
            size="sm"
            options={scopes}
            value={scope}
            change={setScope}
          />
          <SegmentedControl
            label="Verdict"
            size="sm"
            options={verdicts}
            value={verdict}
            change={setVerdict}
          />
        </Row>
        {rows.length === 0 ? (
          <Text tone="muted">No ideas</Text>
        ) : (
          <div ref={focusRow}>
            <Table
              label="Idea list"
              rows={rows}
              rowKey={row => String(row.planNumber)}
              {...(props.selected === null ? {} : {selected: String(props.selected)})}
              select={key => {
                const plan = Number(key);
                props.select(plan === props.selected ? null : plan);
              }}
              middle
              columns={ideaColumns(props.names, props.units)}
            />
          </div>
        )}
        <Row gap="space-4" wrap>
          <Text variant="small" tone="muted">{`${ideas.unplanned} unplanned`}</Text>
          {ideas.truncated ? <Chip label="Some left out" tone="warn" /> : null}
        </Row>
      </Stack>
    </Card>
  );
}
