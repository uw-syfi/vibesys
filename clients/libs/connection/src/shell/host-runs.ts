/**
 * A host's runs as the app reads them: the `runs` subscription as the listing the sidebar, notices
 * and run pages show, and as the count of runs alive. The subscription owns the stream, its lease,
 * its retries and its end; this adds the words of a listing and nothing that can wait.
 */
import type {HostKey, HostListing, ListingStep} from '@vibesys/hosts';
import {LISTING_STEPS} from '@vibesys/hosts';
import {aliveRuns, runs} from '@vibesys/subscription';
import {hostListingFromRuns, listingStep} from '../run-listing.js';
import type {Subscriptions} from './subscriptions.js';

export type HostRunsSource = Pick<Subscriptions, 'observe' | 'first' | 'retry'>;

export interface HostRuns {
  /**
   * `listener` hears the host's listing now and on each change. The first observer of a host starts
   * its subscription and the last one to leave stops it.
   */
  observe(host: HostKey, listener: (listing: HostListing) => void): () => void;
  /**
   * How many runs the host says are alive, once it says so, or the failure it ends with
   * (`SubscriptionFailed`), which is `no_first_frame` if it gives no first frame within the topic's
   * own bound. It is never a count of zero for a host that did not answer, and it never waits for
   * the subscription's attempts to run out.
   */
  alive(host: HostKey, signal: AbortSignal): Promise<number>;
  /** Try again now where a host is waiting or has stopped; a delivering subscription is left be. */
  retry(host: HostKey): void;
}

export function hostRuns(source: HostRunsSource): HostRuns {
  // How far each host's subscription has come, shared by its observers: a window that joins a
  // stream already failed is told where it stopped, as the window that watched it fail was. A
  // new spell starts with a `connecting` state, which sets the step again.
  const steps = new Map<HostKey, ListingStep>();
  return {
    observe(host, listener) {
      let waiting: string | null = null;
      return source.observe(host, runs, state => {
        const step = listingStep(steps.get(host) ?? LISTING_STEPS[0], state);
        steps.set(host, step);
        const listing = hostListingFromRuns(host, state, step);
        // The subscription changes in ways a listing does not show (why a stream was lost, a
        // retry that fails as the one before it did), and a screen is told only what it can see.
        const shown =
          listing.kind === 'unavailable'
            ? JSON.stringify([listing.step, listing.connection])
            : null;
        if (shown !== null && shown === waiting) return;
        waiting = shown;
        listener(listing);
      });
    },
    async alive(host, signal) {
      return aliveRuns(await source.first(host, runs, signal, runs.policy.firstFrameMs));
    },
    retry: host => source.retry(host),
  };
}
