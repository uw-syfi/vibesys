/** Recent: the runs this user opened, each with its status on its host and what it allows. */
import type {JSX, ReactNode} from 'react';
import type {RecentStatus} from '../../../common/recent-status.js';
import {resumeDecision} from '../../../common/resume-run.js';
import {stopKey} from '../../../common/stop-run.js';
import type {ChromeState, WelcomeRecent} from '../../../common/welcome-protocol.js';
import {type ListsState, statusOf} from '../state/lists.js';
import {commands, StopControls, type WelcomeSenders} from './common.js';

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
  readonly run: WelcomeRecent;
  readonly status: RecentStatus;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): ReactNode {
  const {run, status, chrome, send} = props;
  const dispatch = commands(send);
  switch (status.kind) {
    case 'live': {
      const instance = status.instanceId;
      return (
        <>
          <button
            type="button"
            className="primary"
            onClick={() => dispatch({kind: 'attach', host: run.host, instance})}
          >
            Reattach
          </button>
          <StopControls
            view={chrome.stops[stopKey(run.host, instance)]}
            stop={force => dispatch({kind: 'stop', host: run.host, instance, force})}
          />
        </>
      );
    }
    case 'ended': {
      const decision = resumeDecision(run.runId);
      if (decision.kind === 'unavailable') return <span className="muted">{decision.reason}</span>;
      const resume = {host: run.host, project: run.project, run: decision.runId};
      return (
        <button type="button" onClick={() => dispatch({kind: 'resume', ...resume})}>
          Resume…
        </button>
      );
    }
    case 'unknown':
      return (
        <button type="button" onClick={() => send.host({type: 'show', host: run.host})}>
          Open host
        </button>
      );
  }
}

export function RecentPanel(props: {
  readonly lists: ListsState;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {lists, chrome, send} = props;
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
                  <RowActions run={run} status={status} chrome={chrome} send={send} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
