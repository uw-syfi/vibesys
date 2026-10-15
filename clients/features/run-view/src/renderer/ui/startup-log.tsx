/**
 * The startup log panel: a collapsible list of the run's startup output, following the stream on
 * screen. Presentational: it renders the view it is given and reports the user's choices. The body
 * is a reversed flex column, so the browser keeps it scrolled to the newest line unless the reader
 * scrolled up, with no scroll effect of its own.
 */
import type {JSX} from 'react';
import {
  type StartupLogEvent,
  type StartupLogView,
  startupLogNotice,
  startupStreamLabel,
} from '../../state/startup-log.js';

export interface StartupLogPanelProps {
  readonly view: StartupLogView;
  readonly send: (event: StartupLogEvent) => void;
}

export function StartupLogPanel({view, send}: StartupLogPanelProps): JSX.Element {
  const notice = startupLogNotice(view);
  return (
    <section className="panel startup-log" aria-label="Startup log" data-testid="startup-log">
      <div className="panel-heading">
        <button
          type="button"
          className="startup-log-toggle"
          aria-expanded={view.expanded}
          onClick={() => send({type: view.expanded ? 'collapse' : 'expand'})}
        >
          Startup log
        </button>
        {view.expanded && view.streams.length > 1 && (
          <select
            aria-label="Startup step"
            value={view.shown ?? ''}
            onChange={event => send({type: 'select', streamId: event.currentTarget.value})}
          >
            {view.streams.map(stream => (
              <option key={stream.id} value={stream.id}>
                {startupStreamLabel(stream)}
              </option>
            ))}
          </select>
        )}
      </div>
      {view.expanded && (
        <>
          {notice !== null && (
            <p className="startup-log-notice" role="status">
              {notice}
            </p>
          )}
          {view.lines.length > 0 && (
            <div className="startup-log-body" data-testid="startup-log-body">
              <pre>
                {view.earlier && (
                  <span className="startup-log-earlier">… earlier output omitted{'\n'}</span>
                )}
                {view.lines.join('\n')}
              </pre>
            </div>
          )}
        </>
      )}
    </section>
  );
}
