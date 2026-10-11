/** The replay page's "Live gateway URL" form. Presentational: the form's store drives it. */
import type {JSX} from 'react';
import type {GatewayConnectAction, GatewayConnectState} from '../state/gateway-connect.js';

export function GatewayForm({
  state,
  send,
}: {
  readonly state: GatewayConnectState;
  readonly send: (action: GatewayConnectAction) => void;
}): JSX.Element {
  const busy = state.kind !== 'editing';
  return (
    <form
      className="gateway-connect"
      onSubmit={event => {
        event.preventDefault();
        send({type: 'submit'});
      }}
    >
      <label htmlFor="gateway-url">Live gateway URL</label>
      <div className="gateway-connect-row">
        <input
          id="gateway-url"
          type="url"
          value={state.url}
          onChange={event => send({type: 'edit', value: event.target.value})}
          placeholder="http://127.0.0.1:8765/?token=..."
          spellCheck={false}
        />
        <button type="submit" disabled={busy}>
          {busy ? 'Connecting…' : 'Connect'}
        </button>
      </div>
      {state.kind === 'editing' && state.error !== null && (
        <p className="gateway-connect-error">{state.error}</p>
      )}
    </form>
  );
}
