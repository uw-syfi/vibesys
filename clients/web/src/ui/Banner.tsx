import type {Connection} from '../model.js';

export interface BannerProps {
  connection: Connection;
  connectionError: string | null;
  /** The reconnect schedule is exhausted: offer Retry. */
  canRetry: boolean;
  snapshotError: string | null;
  onReconnect: () => void;
  onRetrySnapshot: () => void;
}

// Scaffold; task 9 renders the banner.
export function Banner(_props: BannerProps) {
  return null;
}
