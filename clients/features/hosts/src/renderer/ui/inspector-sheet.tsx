/**
 * Accessible modal view of one detached run, live or ended: its retained identity, and its logs
 * from the run's log store, followed live. It never shows `server.log`, the detached server's raw
 * output, which is for a person at a terminal.
 */

import type {WelcomeInspection} from '@vibesys/ipc';
import {decodeNarrationLine, type LogLine, pipeSegments} from '@vibesys/logs';
import {Button, Dialog} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {inspectionFields} from '../../state/inspector.js';
import {
  type InspectorLogsView,
  type InspectorSheetAction,
  type InspectorSheetState,
  type InspectorStream,
  inspectorLogsNotice,
} from '../../state/inspector-logs.js';

type InspectorSend = (action: InspectorSheetAction) => void;

function inspectionStatus(inspection: WelcomeInspection): {text: string; className: string} {
  if (inspection.state === 'live') {
    return {text: 'The detached server is live.', className: 'status'};
  }
  if (inspection.state === 'ended') {
    return {
      text: 'The detached server has ended. Its retained metadata and logs are shown when available.',
      className: 'status',
    };
  }
  return {
    text: 'The registry could not verify whether this detached server is live.',
    className: 'status warn',
  };
}

function CopyButton(props: {
  readonly label: string;
  readonly value: string;
  readonly disabled: boolean;
  readonly copy: InspectorSend;
}): JSX.Element {
  return (
    <Button
      disabled={props.disabled}
      aria-label={`Copy ${props.label.toLowerCase()}`}
      onClick={() => props.copy({type: 'copy', label: props.label, value: props.value})}
    >
      Copy
    </Button>
  );
}

/** One narration record: time, level, logger and message, and its traceback behind a disclosure. */
function NarrationRecord(props: {readonly text: string}): JSX.Element {
  const record = decodeNarrationLine(props.text);
  if (record.kind === 'raw') {
    return (
      <li className="narration raw">
        <code>{record.text}</code>
      </li>
    );
  }
  const {line} = record;
  return (
    <li className={`narration level-${line.level.toLowerCase()}`} data-level={line.level}>
      <span className="narration-at">{line.at}</span>{' '}
      <span className="narration-level">{line.level}</span>{' '}
      <span className="narration-logger">{line.logger}</span>{' '}
      <span className="narration-message">{line.message}</span>
      {line.traceback == null ? null : (
        <details className="narration-traceback">
          <summary>Traceback</summary>
          <pre>{line.traceback}</pre>
        </details>
      )}
    </li>
  );
}

/** One raw line; what stderr wrote is marked. */
function RawLine(props: {readonly line: LogLine}): JSX.Element {
  return (
    <span className="log-line">
      {pipeSegments(props.line).map(segment =>
        segment.pipe === 'stderr' ? (
          <mark className="stderr" data-pipe="stderr" title="stderr" key={segment.at}>
            {segment.text}
          </mark>
        ) : (
          <span key={segment.at}>{segment.text}</span>
        ),
      )}
      {'\n'}
    </span>
  );
}

function streamOption(stream: InspectorStream): string {
  return `${stream.label} (${stream.status})`;
}

function LogLines(props: {readonly logs: InspectorLogsView}): JSX.Element | null {
  const {logs} = props;
  if (logs.shown === null || logs.lines.length === 0) return null;
  if (logs.shown.narration) {
    return (
      <ol className="log-narration" data-testid="log-narration">
        {logs.lines.map(shown => (
          <NarrationRecord key={shown.number} text={shown.line.text} />
        ))}
      </ol>
    );
  }
  return (
    <pre className="log-raw" data-testid="log-raw">
      {logs.lines.map(shown => (
        <RawLine key={shown.number} line={shown.line} />
      ))}
    </pre>
  );
}

/** The run's log streams, grouped by what they are for, and the one on screen, followed live. */
function InspectionLogs(props: {
  readonly logs: InspectorLogsView;
  readonly send: InspectorSend;
}): JSX.Element | null {
  const {logs, send} = props;
  if (!logs.open) return null;
  const notice = inspectorLogsNotice(logs);
  return (
    <section className="inspection-logs" aria-label="Logs" data-testid="inspection-logs">
      <h3>Logs</h3>
      {logs.groups.length > 0 ? (
        <select
          aria-label="Log stream"
          value={logs.shown?.id ?? ''}
          onChange={event => send({type: 'select-log', streamId: event.currentTarget.value})}
        >
          {logs.groups.map(group => (
            <optgroup key={group.group} label={group.label}>
              {group.streams.map(stream => (
                <option key={stream.id} value={stream.id}>
                  {streamOption(stream)}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      ) : null}
      {notice === null ? null : <p className="muted">{notice}</p>}
      {logs.earlier ? <p className="muted">Earlier output is not shown.</p> : null}
      <LogLines logs={logs} />
    </section>
  );
}

function InspectionBody(props: {
  readonly state: InspectorSheetState;
  readonly send: InspectorSend;
}): JSX.Element | null {
  const {state, send} = props;
  const target = state.target;
  if (target === null) return null;
  let status: ReactNode = null;
  let details: ReactNode = null;
  if (state.load.kind === 'loading') {
    status = <p className="status">Reading one coherent instance observation…</p>;
  } else if (state.load.kind === 'error') {
    status = <p className="status error">{state.load.message}</p>;
  } else if (state.load.kind === 'ready') {
    const inspection = state.load.inspection;
    const summary = inspectionStatus(inspection);
    status = <p className={summary.className}>{summary.text}</p>;
    details = (
      <dl className="inspection">
        {inspectionFields(target, inspection).map(field => (
          <div className="inspection-field" key={field.label}>
            <dt>{field.label}</dt>
            <dd>
              <code>{field.value}</code>
              {field.copy && field.value !== 'Unknown' ? (
                <CopyButton
                  label={field.label}
                  value={field.value}
                  disabled={state.copying !== null}
                  copy={send}
                />
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return (
    <>
      <h2>Inspect {target.runId ?? target.instanceId}</h2>
      <div role="status" aria-live="polite">
        {state.notice === null ? (
          status
        ) : (
          <p className={state.notice.error ? 'status error' : 'status'}>{state.notice.text}</p>
        )}
      </div>
      {details}
      <InspectionLogs logs={state.logs} send={send} />
      <div className="actions">
        <Button disabled={state.load.kind === 'loading'} onClick={() => send({type: 'refresh'})}>
          Refresh
        </Button>
        <Button onClick={() => send({type: 'close'})}>Close</Button>
      </div>
    </>
  );
}

export function InspectorSheet(props: {
  readonly state: InspectorSheetState;
  readonly send: InspectorSend;
}): JSX.Element {
  const target = props.state.target;
  return (
    <Dialog
      open={target !== null}
      label={target === null ? 'Run inspector' : `Inspect ${target.runId ?? target.instanceId}`}
      dismiss={() => props.send({type: 'close'})}
      {...(target === null ? {} : {focusFallbackId: target.focusFallbackId})}
    >
      <InspectionBody state={props.state} send={props.send} />
    </Dialog>
  );
}
