/**
 * The host panel: one host's VibeSys checkout (or the form asking for it), its live runs, and
 * "Start a run…"; or why the host could not be reached, with Try again or Sign in.
 */
import type {FormEvent, JSX, KeyboardEvent, ReactNode} from 'react';
import type {HostKey} from '../../host-settings.js';
import {stopKey} from '../../stop-run.js';
import type {ChromeState, WelcomeHost, WelcomeRun} from '../../welcome-protocol.js';
import {type CheckoutForm, type HostAction, type HostState, hostLabelOf} from '../state/host.js';
import {commands, StopControls, type WelcomeSenders} from './common.js';

function RunRow(props: {
  readonly run: WelcomeRun;
  readonly host: HostKey;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {run, host, chrome} = props;
  const dispatch = commands(props.send);
  return (
    <tr>
      <td>{run.runId ?? run.id}</td>
      <td className="path">{run.projectRoot}</td>
      <td>{run.status}</td>
      <td>{run.startedAt === 0 ? '' : new Date(run.startedAt * 1000).toLocaleString()}</td>
      <td className="actions-cell">
        {run.blocked !== null ? (
          <span className="warn">{run.blocked}</span>
        ) : (
          <>
            <button
              type="button"
              className="primary"
              onClick={() => dispatch({kind: 'attach', host, instance: run.id})}
            >
              Attach
            </button>
            <StopControls
              view={chrome.stops[stopKey(host, run.id)]}
              stop={force => dispatch({kind: 'stop', host, instance: run.id, force})}
            />
          </>
        )}
      </td>
    </tr>
  );
}

function RunTable(props: {
  readonly data: WelcomeHost;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {data, chrome, send} = props;
  if (data.runs.length === 0) return <p className="muted">No live runs on {data.label}.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Run</th>
          <th>Project</th>
          <th>Status</th>
          <th>Started</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {data.runs.map(run => (
          <RunRow key={run.id} run={run} host={data.key} chrome={chrome} send={send} />
        ))}
      </tbody>
    </table>
  );
}

function formStatus(form: CheckoutForm, data: WelcomeHost): string {
  if (form.error !== null) return form.error;
  if (form.checking !== null) return `Checking ${form.value.trim()} on ${data.label}…`;
  if (data.suggestedCheckout !== null && form.value === data.suggestedCheckout) {
    return 'Suggested by a VibeSys server running on this host.';
  }
  return '';
}

/** "Where is your VibeSys checkout on HOST?", checked by the host before it is saved. */
function CheckoutQuestion(props: {
  readonly form: CheckoutForm;
  readonly data: WelcomeHost;
  readonly host: (action: HostAction) => void;
  readonly signIn: ReactNode;
}): JSX.Element {
  const {form, data, host} = props;
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    host({type: 'submit-checkout'});
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape' || !form.cancellable) return;
    event.stopPropagation();
    host({type: 'cancel-checkout'});
  };
  return (
    <form onSubmit={onSubmit} onKeyDown={onKeyDown}>
      <p>Where is your VibeSys checkout on {data.label}?</p>
      <p className="muted">
        The directory you cloned VibeSys into (its pyproject.toml declares the vibesys project). The
        app runs VibeSys there with uv.
      </p>
      <div className="row">
        <input
          // biome-ignore lint/a11y/noAutofocus: the form replaces the line the user clicked (or is the panel's only question), so focus belongs in it, as the page before this one did.
          autoFocus
          value={form.value}
          placeholder="/home/me/src/vibesys"
          spellCheck={false}
          onChange={event => host({type: 'checkout-input', value: event.target.value})}
        />
        <button type="submit" className="primary" disabled={form.checking !== null}>
          Check and save
        </button>
        {form.cancellable ? (
          <button type="button" onClick={() => host({type: 'cancel-checkout'})}>
            Cancel
          </button>
        ) : null}
      </div>
      <p className={form.error === null ? 'status' : 'status error'}>{formStatus(form, data)}</p>
      {form.authNeeded ? props.signIn : null}
    </form>
  );
}

function Ready(props: {
  readonly panel: Extract<HostState['panel'], {kind: 'ready'}>;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
  readonly signIn: ReactNode;
}): JSX.Element {
  const {panel, chrome, send} = props;
  const data = panel.data;
  const host = send.host;
  return (
    <>
      {panel.checkout === null ? (
        <div className="row">
          <span className="muted">VibeSys checkout:</span>
          <code>{data.checkout}</code>
          <button type="button" className="link" onClick={() => host({type: 'edit-checkout'})}>
            Change
          </button>
        </div>
      ) : (
        <CheckoutQuestion form={panel.checkout} data={data} host={host} signIn={props.signIn} />
      )}
      {data.checkout === null ? null : (
        <>
          <RunTable data={data} chrome={chrome} send={send} />
          <div className="row">
            <button
              type="button"
              className="primary"
              onClick={() => send.start({type: 'open', host: data})}
            >
              Start a run…
            </button>
            <button type="button" onClick={() => host({type: 'reload'})}>
              Refresh
            </button>
          </div>
        </>
      )}
    </>
  );
}

function SignIn(props: {
  readonly state: HostState;
  readonly host: (action: HostAction) => void;
}): JSX.Element {
  const signIn = props.state.signIn;
  const label = hostLabelOf(props.state.host);
  let note: ReactNode = null;
  if (signIn !== null) {
    note =
      'error' in signIn ? (
        <p className="error">{signIn.error}</p>
      ) : (
        <p className="muted">Signing in to {label}…</p>
      );
  }
  return (
    <>
      {note}
      <button type="button" className="primary" onClick={() => props.host({type: 'sign-in'})}>
        Sign in to {label}
      </button>
    </>
  );
}

export function HostPanel(props: {
  readonly state: HostState;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {state, chrome, send} = props;
  const label = hostLabelOf(state.host);
  const panel = state.panel;
  const signIn = <SignIn state={state} host={send.host} />;
  let body: ReactNode;
  if (panel.kind === 'loading') {
    body = <p className="muted">Connecting to {label}…</p>;
  } else if (panel.kind === 'failed') {
    body = (
      <>
        <p className="error">{panel.error}</p>
        {panel.authNeeded ? (
          signIn
        ) : (
          <button type="button" onClick={() => send.host({type: 'reload'})}>
            Try again
          </button>
        )}
      </>
    );
  } else {
    body = <Ready panel={panel} chrome={chrome} send={send} signIn={signIn} />;
  }
  return (
    <section id="host-panel">
      <h2>
        {label}
        {state.host === 'local' ? null : (
          <button
            type="button"
            className="link"
            onClick={() => send.host({type: 'show', host: 'local'})}
          >
            Back to This Mac
          </button>
        )}
      </h2>
      {body}
    </section>
  );
}
