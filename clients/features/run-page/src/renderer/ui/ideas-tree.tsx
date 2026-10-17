/**
 * The Ideas face's lineage tree and its controls: the best line over the list, the List | Tree
 * switch, and the tree itself (`ideaTree` rows drawn by the windowed `PickList`).
 */
import {Button, PickList, PickRow, Row, SegmentedControl, Stack, Text} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {
  type BestLine,
  type IdeasLayout,
  type ImplementerOutcomes,
  ideaAncestry,
  ideaTree,
  type RunPageAction,
  type RunPageState,
} from '../../state/index.js';
import {IdeaPickRow} from './idea-pick-row.js';
import type {ViewBase} from './views/props.js';

type Dispatch = (action: RunPageAction) => void;

const LAYOUTS: readonly {readonly value: IdeasLayout; readonly label: string}[] = [
  {value: 'list', label: 'List'},
  {value: 'tree', label: 'Tree'},
];

export function LayoutSwitch(props: {
  readonly layout: IdeasLayout;
  readonly dispatch: Dispatch;
}): JSX.Element {
  return (
    <SegmentedControl
      label="Ideas layout"
      size="sm"
      options={LAYOUTS}
      value={props.layout}
      change={layout => props.dispatch({type: 'ideas-layout', layout})}
    />
  );
}

/** `best: <idea> · N steps from input`; pressing it pins the best idea and lights its chain. */
export function BestLineRow(props: {
  readonly line: BestLine;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {line, dispatch} = props;
  return (
    <Row gap="space-2" align="center" wrap>
      <Text variant="small" tone="muted">
        best:
      </Text>
      <Button
        size="sm"
        variant="quiet"
        text={line.title}
        hint="Show the best idea and the chain that led to it"
        press={() => dispatch({type: 'ideas-pin', idea: line.id})}
      />
      <Text variant="small" tone="muted">
        {`· ${line.steps} ${line.steps === 1 ? 'step' : 'steps'} from input`}
      </Text>
    </Row>
  );
}

export function IdeasTreeList(props: {
  readonly base: ViewBase;
  readonly outcomes: ImplementerOutcomes;
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {base, state, dispatch} = props;
  const {model} = base;
  const {ideas: filter, ideasOpen: open, ideasPinned: pinned, ideasHovered: hovered} = state;
  const tree = useMemo(
    () => ideaTree(model, {filter, open, pinned}),
    [model, filter, open, pinned],
  );
  const lit = useMemo(() => ideaAncestry(model, hovered ?? pinned), [model, hovered, pinned]);
  return (
    <Stack gap="space-2" grow>
      <PickList
        label="Ideas"
        size="fill"
        header={{
          start: 'idea',
          end: [
            {label: 'vs best', bar: true},
            {label: 'vs input', fixed: 'figure' as const},
          ],
        }}
        {...(pinned === null ? {} : {selected: pinned, scrollTo: pinned})}
      >
        {tree.rows.map(entry =>
          entry.kind === 'fold' ? (
            <PickRow
              key={`fold:${entry.key}`}
              id={`fold:${entry.key}`}
              title={entry.label}
              guides={entry.guides}
              fold="none"
              select={() => dispatch({type: 'ideas-open', key: entry.key, open: true})}
            />
          ) : (
            <IdeaPickRow
              key={entry.row.id}
              row={entry.row}
              outcomes={props.outcomes}
              compact={false}
              now={base.clock.now}
              zoom={base.zoom}
              place={{
                guides: entry.guides,
                depth: entry.depth,
                summary: entry.summary,
                fold:
                  entry.below === 'closed' || entry.below === 'open'
                    ? {
                        open: entry.below === 'open',
                        toggle: () =>
                          dispatch({
                            type: 'ideas-open',
                            key: entry.row.id,
                            open: entry.below === 'closed',
                          }),
                      }
                    : 'none',
                lit: lit.has(entry.row.id),
                hover: on => dispatch({type: 'ideas-hover', idea: on ? entry.row.id : null}),
              }}
            />
          ),
        )}
      </PickList>
      {tree.hiddenFailed === 0 ? null : (
        <Button
          size="sm"
          variant="quiet"
          text={`+ ${tree.hiddenFailed} failed`}
          press={() => dispatch({type: 'ideas-filter', filter: {...filter, failed: true}})}
        />
      )}
    </Stack>
  );
}
