/** Recent: the runs this user opened, each with its status on its host and what it allows. */

import {
  type HostKey,
  type RecentRun,
  type RecentStatus,
  type ResumeRunId,
  resumeDecision,
} from '@vibesys/entities';
import {Button} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {type ListsState, statusOf} from '../../state/lists.js';

/**
 * What a recent run offers, which other features own: reattaching, resuming, its stop controls,
 * and opening its host's panel. The app composes them in.
 */
export interface RecentRunActions {
  readonly attach: (host: HostKey, instance: string) => void;
  readonly resume: (host: HostKey, project: string, run: ResumeRunId) => void;
  readonly stopControls: (host: HostKey, instance: string) => ReactNode;
  readonly openHost: (host: HostKey) => void;
}

function statusText(status: RecentStatus): {text: string; className: string} {
  switch (status.kind) {
    case 'live':
      return {text: status.status, className: 'ok'};
    case 'ended': {
      const how = status.status ?? 'ended';
      return {text: status.reason === null ? how : `${how}: ${status.reason}`, className: 'muted'};
    }
    case 'unknown':
      return {text: 'checking…', className: 'muted'};
  }
}

function RowActions(props: {
  readonly run: RecentRun;
  readonly status: RecentStatus;
  readonly actions: RecentRunActions;
}): ReactNode {
  const {run, status, actions} = props;
  switch (status.kind) {
    case 'live': {
      const instance = status.instanceId;
      return (
        <>
          <Button variant="primary" onClick={() => actions.attach(run.host, instance)}>
            Reattach
          </Button>
          {actions.stopControls(run.host, instance)}
        </>
      );
    }
    case 'ended': {
      const decision = resumeDecision(run.runId);
      if (decision.kind === 'unavailable') return <span className="muted">{decision.reason}</span>;
      const resume = (): void => actions.resume(run.host, run.project, decision.runId);
      return <Button onClick={resume}>Resume…</Button>;
    }
    case 'unknown':
      return <Button onClick={() => actions.openHost(run.host)}>Open host</Button>;
  }
}

export function RecentPanel(props: {
  readonly lists: ListsState;
  readonly actions: RecentRunActions;
}): JSX.Element {
  const {lists, actions} = props;
  if (lists.runs.length === 0) {
    return (
      <section id="recent-section">
        <h2>Recent</h2>
        <p className="muted">No runs yet. Runs you attach to appear here.</p>
      </section>
    );
  }
  return (
    <section id="recent-section">
      <h2>Recent</h2>
      <table>
        <thead>
          <tr>
            <th>Host</th>
            <th>Project</th>
            <th>Task</th>
            <th>Run</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {lists.runs.map(run => {
            const status = statusOf(lists, run);
            const {text, className} = statusText(status);
            return (
              <tr key={`${run.host}\n${run.runId}`}>
                <td>{run.hostLabel}</td>
                <td className="path">{run.project}</td>
                <td>{run.task ?? '–'}</td>
                <td>{run.runId}</td>
                <td className={className}>{text}</td>
                <td className="actions-cell">
                  <RowActions run={run} status={status} actions={actions} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
