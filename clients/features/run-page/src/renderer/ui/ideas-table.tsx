/** A flat, windowed list of ideas. Ordering and filtering are presentation-only. */
import {markdownToPlain, PickList, PickRow, Stack} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {
  ageWords,
  type IdeaRow,
  type IdeasFilter,
  type ImplementerOutcomes,
  ideasView,
  type RunModel,
  type RunPageAction,
} from '../../state/index.js';
import type {ViewBase} from './views/props.js';

export type IdeasSort = 'newest' | 'oldest' | 'result';

function outcomeOf(row: IdeaRow, outcomes: ImplementerOutcomes): string {
  if (row.outcome === 'failed') return row.state.label.replace(/^failed(?: · )?/, '') || 'failed';
  if (row.outcome === 'running') return 'running';
  return outcomes.get(row.id)?.outcome?.label ?? 'passed';
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

export function sortedIdeas(model: RunModel, filter: IdeasFilter, sort: IdeasSort) {
  const rows = ideasView(model, filter).rows;
  const order = new Map(model.candidates.candidates.map((item, index) => [item.id, index]));
  if (sort === 'newest')
    return [...rows].sort((a, b) => (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0));
  if (sort === 'oldest')
    return [...rows].sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
  const values = new Map(
    model.candidates.candidates.map(item => [item.id, item.measurement?.vsBaseline ?? null]),
  );
  return [...rows].sort((a, b) => byResult(a, b, values, order, model.direction));
}

export function IdeasTable(props: {
  readonly base: ViewBase;
  readonly filter: IdeasFilter;
  readonly sort: IdeasSort;
  readonly outcomes: ImplementerOutcomes;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {base, filter, sort} = props;
  const rows = useMemo(() => sortedIdeas(base.model, filter, sort), [base.model, filter, sort]);
  return (
    <Stack grow>
      <PickList
        label="Ideas"
        header={{
          start: 'idea',
          end: [
            {label: 'outcome', fixed: 'measure-note'},
            {label: 'vs input', fixed: 'figure'},
          ],
        }}
      >
        {rows.map(row => {
          const adopted = row.badges.some(badge => badge.label === 'adopted');
          const outcome = outcomeOf(row, props.outcomes);
          const measured = row.figure.endsWith('×');
          return (
            <PickRow
              key={row.id}
              id={row.id}
              title={`${adopted ? '★ ' : ''}${markdownToPlain(row.title)}`}
              mono
              state={row.state}
              measure={{label: outcome, size: 'note', title: outcome}}
              measureBeforeFigure
              figure={
                row.runningSince === null
                  ? measured
                    ? row.figure
                    : '–'
                  : ageWords(row.runningSince, base.clock.now)
              }
              figureTone={measured ? row.figureTone : 'muted'}
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
