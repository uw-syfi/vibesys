/** A flat, windowed list of ideas. Ordering and filtering are presentation-only. */
import {EmptyState, markdownToPlain, PickList, PickRow, type RowBadge, Stack} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {vsInputCell} from '../../state/idea-words.js';
import {
  activityLine,
  activityNow,
  ageWords,
  baselineColumns,
  effectWords,
  type IdeaEffects,
  type IdeaRow,
  type IdeasFilter,
  type ImplementerOutcomes,
  ideaActivities,
  ideasView,
  noIdeasWords,
  type RunModel,
  type RunPageAction,
  summaryLine,
} from '../../state/index.js';
import type {ViewBase} from './views/props.js';

export type IdeasSort = 'newest' | 'oldest' | 'result';

const capitalized = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

/** What an idea built on, as the line under its title words it; empty while it is planned. */
function fromWords(row: IdeaRow): string {
  const {kind, label} = row.trail;
  if (kind === 'own work') return 'from its own work';
  return label === '…' ? '' : `from ${label}`;
}

function effectOf(id: string, effects: IdeaEffects): string {
  const effect = effects.get(id);
  return effect === undefined ? '' : effectWords(effect);
}

/**
 * The line under an idea's title: what its status badge does not say. While it runs, what it is
 * doing (`live`, the activity line). Once settled, the first sentence of what its implementer
 * reported, then what it built on (`from best`), the framework's verdict on its effect against
 * that base (`within noise of its base`), why it failed when the span says, and the implementer's
 * outcome when it reported no summary.
 */
export function subtitleOf(
  row: IdeaRow,
  outcomes: ImplementerOutcomes,
  effects: IdeaEffects,
  live: string | null = null,
): string {
  if (row.outcome === 'running' && live !== null) return live;
  const why = row.state.label.replace(/^failed(?:\s*·\s*)?/i, '').trim();
  const reported = outcomes.get(row.id);
  const summary =
    reported?.summary == null ? '' : markdownToPlain(summaryLine(reported.summary)).trim();
  const parts = [
    summary,
    fromWords(row),
    effectOf(row.id, effects),
    row.outcome === 'failed' ? why : '',
    summary === '' ? (reported?.outcome?.label ?? '') : '',
  ];
  return capitalized(parts.filter(part => part !== '').join(' · '));
}

function badgeOf(row: IdeaRow): readonly RowBadge[] {
  if (row.badges.some(badge => badge.label === 'adopted')) return [{label: 'Adopted', tone: 'ok'}];
  if (row.badges.some(badge => badge.label === 'best')) return [{label: 'Best', tone: 'ok'}];
  if (row.outcome === 'failed') return [{label: 'Failed', tone: 'err'}];
  if (row.outcome === 'running') return [{label: 'Running', tone: 'live'}];
  return [{label: 'Passed', tone: 'ok'}];
}

function byResult(
  a: IdeaRow,
  b: IdeaRow,
  values: ReadonlyMap<string, number | null>,
  order: ReadonlyMap<string, number>,
  direction: RunModel['direction'],
): number {
  const av = values.get(a.id) ?? null;
  const bv = values.get(b.id) ?? null;
  const newest = (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0);
  if (av === null) return bv === null ? newest : 1;
  if (bv === null) return -1;
  return (direction === 'min' ? av - bv : bv - av) || newest;
}

function sortedIdeas(model: RunModel, filter: IdeasFilter, sort: IdeasSort) {
  const rows = ideasView(model, filter).rows;
  const order = new Map(model.candidates.candidates.map((item, index) => [item.id, index]));
  if (sort === 'newest')
    return [...rows].sort((a, b) => (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0));
  if (sort === 'oldest')
    return [...rows].sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
  const values = new Map(
    model.candidates.candidates.map(item => [
      item.id,
      item.measurement?.vsBaseline ?? item.measurement?.against?.ratio ?? null,
    ]),
  );
  return [...rows].sort((a, b) => byResult(a, b, values, order, model.direction));
}

export function IdeasTable(props: {
  readonly base: ViewBase;
  readonly filter: IdeasFilter;
  readonly sort: IdeasSort;
  readonly selected: string | null;
  readonly outcomes: ImplementerOutcomes;
  readonly effects: IdeaEffects;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {base, filter, sort} = props;
  const rows = useMemo(() => sortedIdeas(base.model, filter, sort), [base.model, filter, sort]);
  const columns = useMemo(() => baselineColumns(base.model), [base.model]);
  const activities = useMemo(() => ideaActivities(base.model), [base.model]);
  const now = activityNow(activities, base.clock.now);
  if (rows.length === 0) {
    const words = noIdeasWords(filter);
    return <EmptyState title={words.title} body={words.body} />;
  }
  return (
    <Stack grow>
      <PickList
        wrapSummary
        size="fill"
        label="Ideas"
        {...(props.selected === null ? {} : {selected: props.selected})}
        header={{
          start: 'idea',
          end: [
            {label: 'vs input', fixed: 'figure'},
            ...columns.labels.map(label => ({label, fixed: 'measure'}) as const),
          ],
        }}
      >
        {rows.map(row => {
          const activity = activities.get(row.id);
          const subtitle = subtitleOf(
            row,
            props.outcomes,
            props.effects,
            activity === undefined ? null : activityLine(activity, now),
          );
          const cell = vsInputCell({figure: row.figure, tone: row.figureTone});
          return (
            <PickRow
              key={row.id}
              id={row.id}
              title={markdownToPlain(row.title)}
              {...(subtitle === '' ? {} : {summary: subtitle})}
              stacked
              mono
              badges={badgeOf(row)}
              figure={
                row.runningSince === null ? cell.figure : ageWords(row.runningSince, base.clock.now)
              }
              {...(row.runningSince === null &&
              cell.figure === row.figure &&
              row.figureTitle !== undefined
                ? {figureTitle: row.figureTitle}
                : {})}
              figureTone={cell.tone}
              measure={(columns.cells.get(row.id) ?? []).map(label => ({label}))}
              fresh={row.fresh}
              hover={on => props.dispatch({type: 'ideas-hover', idea: on ? row.id : null})}
              select={() => base.zoom({type: 'open-idea', idea: row.id})}
            />
          );
        })}
      </PickList>
    </Stack>
  );
}
