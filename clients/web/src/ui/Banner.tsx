import {RefreshCw, WifiOff} from 'lucide-react';
import type {Connection} from '../model.js';
import './Banner.css';

export interface BannerProps {
  connection: Connection;
  connectionError: string | null;
  /** The reconnect schedule is exhausted: offer Retry. */
  canRetry: boolean;
  snapshotError: string | null;
  onReconnect: () => void;
  onRetrySnapshot: () => void;
}

const icon = {size: 16, strokeWidth: 1.75, 'aria-hidden': true} as const;

/** Full-width connection and liveness problems. Nothing renders while all is well. */
export function Banner(props: BannerProps) {
  const {connection, connectionError, canRetry, snapshotError, onReconnect, onRetrySnapshot} =
    props;
  if (connection === 'error') {
    return (
      <div className="banner" role="alert">
        <WifiOff {...icon} />
        <span>Backend protocol error: {connectionError}</span>
        <button type="button" className="btn btn-sm" onClick={onReconnect}>
          Retry
        </button>
      </div>
    );
  }
  if (connection === 'disconnected') {
    return canRetry ? (
      <div className="banner" role="alert">
        <WifiOff {...icon} />
        <span>Disconnected from the backend.</span>
        <button type="button" className="btn btn-sm" onClick={onReconnect}>
          Retry
        </button>
      </div>
    ) : (
      <div className="banner">
        <RefreshCw {...icon} className="spin" />
        <span>Reconnecting…</span>
      </div>
    );
  }
  if (snapshotError !== null) {
    return (
      <div className="banner" role="alert">
        <span>Could not load run status: {snapshotError}</span>
        <button type="button" className="btn btn-sm" onClick={onRetrySnapshot}>
          Retry
        </button>
      </div>
    );
  }
  return null;
}
