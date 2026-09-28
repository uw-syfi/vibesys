import {type FormEvent, useState} from 'react';
import './GatewayConnect.css';

/** Points the page at a live gateway by reloading it with `?gateway=<capability URL>`. */
export function GatewayConnect() {
  const [gatewayUrl, setGatewayUrl] = useState('');
  const [error, setError] = useState<string | null>(null);

  const connect = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    try {
      const gateway = new URL(gatewayUrl.trim(), window.location.origin);
      if (!['http:', 'https:'].includes(gateway.protocol)) {
        throw new Error('Use an http:// or https:// gateway URL');
      }
      if (window.location.protocol === 'https:' && gateway.protocol !== 'https:') {
        throw new Error('An HTTPS browser page requires an HTTPS gateway URL');
      }
      if (!gateway.searchParams.has('token')) {
        throw new Error('The gateway URL must include its capability token');
      }
      const page = new URL(window.location.href);
      page.search = new URLSearchParams({gateway: gateway.toString()}).toString();
      window.location.assign(page.toString());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <form className="gateway-connect" onSubmit={connect}>
      <label htmlFor="gateway-url">Replay. Live gateway URL</label>
      <input
        id="gateway-url"
        type="url"
        value={gatewayUrl}
        onChange={event => setGatewayUrl(event.target.value)}
        placeholder="http://127.0.0.1:8765/?token=..."
        spellCheck={false}
      />
      <button type="submit">Connect</button>
      {error !== null && (
        <p className="gateway-connect-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
