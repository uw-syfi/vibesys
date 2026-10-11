/** Recent: the runs this user opened, each with its status on its host and what it allows. */
import type {JSX, ReactNode} from 'react';
import {resumeDecision} from '../../resume-run.js';
import {stopKey} from '../../stop-run.js';
import type {ChromeState, WelcomeRecent, WelcomeRecentStatus} from '../../welcome-protocol.js';
import {type RecentState, statusOf} from '../state/recent.js';
import {commands, StopControls, type WelcomeSenders} from './common.js';

function statusText(status: WelcomeRecentStatus | undefined): {text: string; className: string} {
  if (status === undefined) return {text: 'checking…', className: 'muted'};
  if (status.kind === 'live') return {text: `running (${status.status})`, className: 'ok'};
  if (status.kind === 'ended') return {text: 'ended', className: 'muted'};
  return {text: `unknown: ${status.detail}`, className: 'warn'};
}

function RowActions(props: {
  readonly run: WelcomeRecent;
  readonly status: WelcomeRecentStatus | undefined;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): ReactNode {
  const {run, status, chrome, send} = props;
  const dispatch = commands(send);
  switch (status?.kind) {
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
    default:
      return null;
  }
}

export function RecentPanel(props: {
  readonly recent: RecentState;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {recent, chrome, send} = props;
  if (recent.runs.length === 0) {
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
          {recent.runs.map(run => {
            const status = statusOf(recent, run);
            const {text, className} = statusText(status);
            return (
              <tr key={`${run.host}\n${run.instanceId}`}>
                <td>{run.hostLabel}</td>
                <td className="path">{run.project}</td>
                <td>{run.task ?? '–'}</td>
                <td>{run.runId ?? run.instanceId}</td>
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
