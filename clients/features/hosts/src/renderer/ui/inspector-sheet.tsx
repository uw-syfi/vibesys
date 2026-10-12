/** Accessible modal view of one detached server's retained identity and optional log tail. */

import type {WelcomeInspection} from '@vibesys/ipc';
import {Button, Dialog} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  type InspectorAction,
  type InspectorState,
  inspectionFields,
} from '../../state/inspector.js';

type InspectorSend = (action: InspectorAction) => void;

function inspectionStatus(inspection: WelcomeInspection): {text: string; className: string} {
  if (inspection.state === 'live') {
    return {text: 'The detached server is live.', className: 'status'};
  }
  if (inspection.state === 'ended') {
    return {
      text: 'The detached server has ended. Retained metadata and diagnostics are shown when available.',
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

function InspectionBody(props: {
  readonly state: InspectorState;
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
      <>
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
        <InspectionLog inspection={inspection} copying={state.copying !== null} send={send} />
      </>
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
      <div className="actions">
        {!state.includeLog ? (
          <Button disabled={state.load.kind === 'loading'} onClick={() => send({type: 'load-log'})}>
            Load diagnostics
          </Button>
        ) : null}
        <Button disabled={state.load.kind === 'loading'} onClick={() => send({type: 'refresh'})}>
          Refresh
        </Button>
        <Button onClick={() => send({type: 'close'})}>Close</Button>
      </div>
    </>
  );
}

function InspectionLog(props: {
  readonly inspection: WelcomeInspection;
  readonly copying: boolean;
  readonly send: InspectorSend;
}): JSX.Element {
  const log = props.inspection.log;
  if (log == null) {
    return <p className="muted">Diagnostics are not loaded until you request them.</p>;
  }
  if (log.state === 'missing') {
    return <p className="muted">No retained server log is available.</p>;
  }
  return (
    <div>
      <p className="muted">
        {log.truncated ? 'Server log tail (earlier bytes omitted)' : 'Server log'}
      </p>
      <pre className="diagnostics">{log.text}</pre>
      <CopyButton label="Diagnostics" value={log.text} disabled={props.copying} copy={props.send} />
    </div>
  );
}

export function InspectorSheet(props: {
  readonly state: InspectorState;
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
