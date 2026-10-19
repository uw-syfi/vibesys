/**
 * The Logs face, per level: the run's streams grouped by owner, the streams of an idea's spans, or
 * one step's. A row per stream shows its owner and last line; the chosen stream fills a `LogPane`.
 * Streams come from the page state, joined to spans by the stream owner's `span_id`
 * (`state/scoped-logs.ts`). The shell follows the shown stream (`streamToFollow`): a view runs no effects.
 */
import {EmptyState, LogPane, PickList, PickRow} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {type RunPageAction, type RunPageState, stepKicker} from '../../../../state/index.js';
import {
  ideaTitlesBySpan,
  type LogScope,
  paneLines,
  type ScopedLogs,
  scopedLogs,
} from '../../../../state/scoped-logs.js';
import type {PaneHeader, ViewBase, ViewHeader} from '../props.js';

export type {LogScope};

export interface LogsViewProps extends ViewBase {
  readonly state: RunPageState;
  readonly scope: LogScope;
  readonly dispatch: (action: RunPageAction) => void;
}

/** A step's Logs pane keeps the step pane's kicker, so the role is not said twice over its title. */
export const logsHeader: ViewHeader<LogsViewProps> = ({scope}): PaneHeader =>
  scope.kind === 'step' ? {kicker: stepKicker(scope.step)} : {};

type Dispatch = (action: RunPageAction) => void;
type Row = ScopedLogs['rows'][number];

function liveState(live: boolean): {tone: 'live' | 'neutral'; label: string} {
  return live ? {tone: 'live', label: 'Writing'} : {tone: 'neutral', label: 'Closed'};
}

/**
 * The repeats of a startup step in one row: how long the step took in all, when the run reported
 * it, and how many times it ran. Choosing the row shows the newest and lists them all.
 */
function FoldRow(props: {
  readonly id: string;
  readonly row: Extract<Row, {kind: 'fold'}>;
  readonly byOwner: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {row, dispatch} = props;
  return (
    <PickRow
      id={row.id}
      title={row.label}
      figure={[row.took, `×${row.count}`].filter(part => part !== null).join(' ')}
      figureTone="muted"
      {...(row.lastLine === null ? {} : {summary: row.lastLine})}
      {...(props.byOwner ? {lead: 'Startup'} : {})}
      state={liveState(row.live)}
      select={() => dispatch({type: 'select-stream', streamId: row.latest})}
    />
  );
}

function StreamRow(props: {
  readonly id: string;
  readonly row: Extract<Row, {kind: 'stream'}>;
  readonly byOwner: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {row, dispatch} = props;
  const {stream} = row;
  return (
    <PickRow
      id={stream.id}
      title={stream.label}
      {...(stream.lastLine === null ? {} : {summary: stream.lastLine})}
      {...(props.byOwner && !row.nested && stream.lead !== null ? {lead: stream.lead} : {})}
      {...(row.nested ? {guides: ['tee']} : {})}
      {...(row.nested || stream.took === null ? {} : {figure: stream.took, figureTone: 'muted'})}
      state={liveState(stream.live)}
      select={() => dispatch({type: 'select-stream', streamId: stream.id})}
    />
  );
}

function StreamRows(props: {
  readonly logs: ScopedLogs;
  readonly byOwner: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {byOwner, dispatch} = props;
  return (
    <PickList
      label="Log streams"
      {...(props.logs.shown === null ? {} : {selected: props.logs.shown})}
    >
      {props.logs.rows.map(row =>
        row.kind === 'fold' ? (
          <FoldRow key={row.id} id={row.id} row={row} byOwner={byOwner} dispatch={dispatch} />
        ) : (
          <StreamRow
            key={row.stream.id}
            id={row.stream.id}
            row={row}
            byOwner={byOwner}
            dispatch={dispatch}
          />
        ),
      )}
    </PickList>
  );
}

export function LogsView(props: LogsViewProps): JSX.Element {
  const {state, scope, dispatch} = props;
  const ideas = useMemo(
    () => ideaTitlesBySpan(props.model.candidates.candidates),
    [props.model.candidates],
  );
  const logs = scopedLogs(state, scope, ideas);
  const {shown} = logs;
  if (logs.notice !== null) return <EmptyState title="No logs to show" body={logs.notice} />;
  const stream = logs.streams.find(item => item.id === shown);
  if (stream === undefined) return <EmptyState title="No logs to show" body="Choose a stream." />;
  const pane = (
    <LogPane
      key={stream.id}
      label={stream.label}
      lines={paneLines(state, shown)}
      size="fill"
      start={stream.live ? 'tail' : 'top'}
    />
  );
  if (scope.kind !== 'run' && logs.streams.length === 1) return pane;
  // The pane body is a column: the stream rows take their own height, the log fills the rest.
  return (
    <>
      <StreamRows logs={logs} byOwner={scope.kind === 'run'} dispatch={dispatch} />
      {pane}
    </>
  );
}
