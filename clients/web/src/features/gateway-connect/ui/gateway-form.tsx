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
  const connecting = state.kind === 'connecting';
  const connected = state.kind === 'connected';
  const canRetry = state.kind === 'editing' && state.retryTarget !== null;
  const error = state.kind === 'editing' ? state.error : null;
  const announcement = connecting ? 'Connecting…' : (error ?? '');
  return (
    <form
      className="gateway-connect"
      onSubmit={event => {
        event.preventDefault();
        send({type: canRetry ? 'retry' : 'submit'});
      }}
    >
      <label htmlFor="gateway-url">Live gateway URL</label>
      <div className="gateway-connect-row">
        <input
          id="gateway-url"
          type="url"
          value={state.url}
          onChange={event => send({type: 'edit', value: event.target.value})}
          disabled={connecting || connected}
          placeholder="http://127.0.0.1:8765/?token=..."
          spellCheck={false}
        />
        {connecting ? (
          <button type="button" onClick={() => send({type: 'cancel'})}>
            Cancel
          </button>
        ) : (
          <button type="submit" disabled={connected}>
            {connected ? 'Connected' : canRetry ? 'Retry' : 'Connect'}
          </button>
        )}
      </div>
      <p
        className={error === null ? 'gateway-connect-status' : 'gateway-connect-error'}
        role="status"
        aria-live="polite"
        aria-atomic={true}
      >
        {announcement}
      </p>
    </form>
  );
}
