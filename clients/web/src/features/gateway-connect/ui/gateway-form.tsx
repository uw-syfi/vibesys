/** The replay page's "Live gateway URL" form. Presentational: the form's store drives it. */

import {Button} from '@vibesys/ui';
import type {JSX} from 'react';
import type {GatewayConnectAction, GatewayConnectState} from '../state/gateway-connect.js';

const MESSAGE_ID = 'gateway-url-message';

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
  const feedback = state.kind === 'editing' ? state.feedback : null;
  const announcement = connecting ? 'Connecting…' : (feedback?.message ?? '');
  const invalid = feedback?.kind === 'failure';
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
          aria-describedby={announcement === '' ? undefined : MESSAGE_ID}
          aria-invalid={invalid ? true : undefined}
          placeholder="http://127.0.0.1:8765/?token=..."
          spellCheck={false}
        />
        {connecting ? (
          <Button
            onClick={event => {
              // This button becomes the form's submit button during the same click. Prevent the
              // browser's post-handler default action from submitting the newly rendered Retry.
              event.preventDefault();
              send({type: 'cancel'});
            }}
          >
            Cancel
          </Button>
        ) : (
          <Button type="submit" disabled={connected}>
            {connected ? 'Connected' : canRetry ? 'Retry' : 'Connect'}
          </Button>
        )}
      </div>
      <p
        id={MESSAGE_ID}
        className={invalid ? 'gateway-connect-error' : 'gateway-connect-status'}
        role="status"
        aria-live="polite"
        aria-atomic={true}
      >
        {announcement}
      </p>
    </form>
  );
}
