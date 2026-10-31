/**
 * What the host says of one run, as the run reader takes it: the host's `runs` subscription,
 * observed for that run's key through the window's port to main. Main holds the stream, its lease
 * and its retries; the reader hears whether the host is delivering and, if so, the run's liveness.
 */
import type {HostKey} from '@vibesys/hosts';
import type {RunLivenessFeed, RunLivenessState} from '@vibesys/run-skeleton';
import {
  type HostRunFacts,
  runs,
  type SubscriptionPort,
  type SubscriptionState,
  subscriptionSource,
} from '@vibesys/subscription';

/** The reader's state for `runId` from the subscription's: only a live subscription is a word. */
export function livenessOf(
  runId: string,
  state: SubscriptionState<HostRunFacts>,
): RunLivenessState {
  switch (state.kind) {
    case 'connecting':
      // Not yet delivering is not lost, unless an earlier stream was.
      return state.attempt === 0 ? {kind: 'waiting'} : {kind: 'lost'};
    case 'live': {
      const facts = state.value.get(runId);
      return {
        kind: 'live',
        hint:
          facts === undefined
            ? undefined
            : {epoch: facts.head?.epoch ?? 0, value: facts.liveness, head: facts.head},
      };
    }
    case 'stale':
    case 'failed':
      return {kind: 'lost'};
  }
}

/** The liveness of each run of `host`, which the reader asks for by run id. */
export function runLiveness(
  port: SubscriptionPort,
  host: HostKey,
): (runId: string) => RunLivenessFeed {
  return runId => {
    const source = subscriptionSource(port, host, runs, runId);
    let last: {readonly from: SubscriptionState<HostRunFacts>; readonly state: RunLivenessState};
    return {
      subscribe: source.subscribe,
      // The same object until the state changes, as a store the reader subscribes to must give.
      getSnapshot: () => {
        const from = source.getSnapshot();
        if (last === undefined || last.from !== from) last = {from, state: livenessOf(runId, from)};
        return last.state;
      },
    };
  };
}
