/**
 * The Logs face, per level: the run's streams, the streams of an idea's spans, or one step's.
 * Streams come from the page state (`state.logs`, `state.views`), joined to spans by the stream
 * owner's `span_id`.
 *
 * Stub: every scope shows the run's streams, grouped by owner, and the one chosen; scoping to an
 * idea or a step is still to build (#2997).
 */
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {EmptyState, LogPane, Split, Tree, TreeRow} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  logGroups,
  logsNotice,
  type RunPageAction,
  type RunPageState,
  shownStream,
  streamLines,
} from '../../../../state/index.js';
import type {ViewBase} from '../props.js';

/** Whose logs: the run's, one idea's, or one step's. */
type LogScope =
  | {readonly kind: 'run'}
  | {readonly kind: 'idea'; readonly idea: Candidate}
  | {readonly kind: 'step'; readonly step: CandidateStep};

export interface LogsViewProps extends ViewBase {
  readonly state: RunPageState;
  readonly scope: LogScope;
  readonly dispatch: (action: RunPageAction) => void;
}

function StreamTree(props: {
  readonly state: RunPageState;
  readonly shown: string | null;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {dispatch} = props;
  return (
    <Tree label="Log streams" {...(props.shown === null ? {} : {selected: props.shown})}>
      {logGroups(props.state).flatMap(group => [
        <TreeRow
          key={`group ${group.name}`}
          id={`group ${group.name}`}
          depth={0}
          title={group.name}
          expanded
          select={() => {
            const first = group.streams[0];
            if (first !== undefined) dispatch({type: 'select-stream', streamId: first.id});
          }}
        />,
        ...group.streams.map(stream => (
          <TreeRow
            key={stream.id}
            id={stream.id}
            depth={1}
            title={stream.label}
            state={
              stream.live ? {tone: 'live', label: 'Writing'} : {tone: 'neutral', label: 'Closed'}
            }
            select={() => dispatch({type: 'select-stream', streamId: stream.id})}
          />
        )),
      ])}
    </Tree>
  );
}

export function LogsView(props: LogsViewProps): JSX.Element {
  const {state, dispatch} = props;
  const notice = logsNotice(state);
  const groups = logGroups(state);
  const shown = shownStream(state);
  if (notice !== null && groups.length === 0) {
    return <EmptyState title="No logs to show" body={notice} />;
  }
  const label =
    groups.flatMap(group => group.streams).find(stream => stream.id === shown)?.label ?? 'Log';
  return (
    <Split
      main={<LogPane label={label} lines={streamLines(state, shown)} size="fill" />}
      aside={<StreamTree state={state} shown={shown} dispatch={dispatch} />}
    />
  );
}
