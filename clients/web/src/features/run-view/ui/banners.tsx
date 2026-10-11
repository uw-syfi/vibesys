import type {JSX} from 'react';
import type {ConnectionBanners} from '../state/banners.js';

/*
  Each banner carries an `aria-label` as well as a `data-testid`. They are different mechanisms for
  different audiences: `data-testid` reaches tests and has no ARIA mapping at all, while the label
  is what names the region in the accessibility tree. Without one, regions that share
  `role="alert"` and a class are indistinguishable to a screen reader, and `role="alert"` implies
  `aria-live="assertive"`, so two that are live at once interrupt each other unnamed.
*/

export interface BannersProps {
  readonly banners: ConnectionBanners;
  /** Why the replay fixture failed to load, or null. */
  readonly replayFailure: string | null;
  readonly onReattach: () => void;
  readonly onReconnectControls: () => void;
  readonly onRetryReplay: () => void;
}

export function Banners({
  banners,
  replayFailure,
  onReattach,
  onReconnectControls,
  onRetryReplay,
}: BannersProps): JSX.Element {
  return (
    <>
      {banners.page !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Page connection status"
          data-testid="page-connection-banner"
        >
          <span>{banners.page.message}</span>
        </div>
      )}
      {banners.stream !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Event stream status"
          data-testid="stream-banner"
        >
          <span>{banners.stream.message}</span>
          {banners.stream.reattach && (
            <button type="button" onClick={onReattach}>
              Reattach
            </button>
          )}
        </div>
      )}
      {banners.controls !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Run controls status"
          data-testid="controls-banner"
        >
          <span>{banners.controls.message}</span>
          {/*
            Disabled rather than hidden while a dial is in flight: a redial no-ops then, so an
            enabled button would swallow clicks for the whole connect-timeout window and change
            nothing on screen. Hiding it instead would make the affordance flicker in and out.
          */}
          <button type="button" disabled={banners.controls.retrying} onClick={onReconnectControls}>
            {banners.controls.retrying ? 'Reconnecting…' : 'Reconnect now'}
          </button>
        </div>
      )}
      {replayFailure !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Replay status"
          data-testid="replay-banner"
        >
          <span>Replay failed to load: {replayFailure}</span>
          <button type="button" onClick={onRetryReplay}>
            Retry
          </button>
        </div>
      )}
    </>
  );
}
