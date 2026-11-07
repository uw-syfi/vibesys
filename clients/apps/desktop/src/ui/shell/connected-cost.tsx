/**
 * The Cost page bound to the hosts' services: while it shows, every host's cost stream for the whole
 * of its history is followed (leaving the page closes them), and each host's served view, status and
 * feed are read from its session. The page computes nothing; a run row opens that run's report.
 */
import {useAtomMount, useAtomValue} from '@effect/atom-react';
import * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo} from 'react';
import {hostStatusOf, staleForMs} from '../../state/index.js';
import {type CostOverviewHost, CostOverviewScreen} from '../cost-overview/index.js';
import {nowMs} from '../orchestrator/clock.js';
import type {ConnectedHost, Dispatch} from '../orchestrator/connected.js';
import {COST_OVERVIEW_WORDS} from '../orchestrator/cost-words.js';

/** The cost streams of every host, followed while this is mounted. */
const watchCosts = (hosts: readonly ConnectedHost[]): Atom.Atom<void> =>
  Atom.make(get => {
    for (const host of hosts) get(host.session.hostCosts.watch('all'));
  });

/** Each host as the Cost page reads it. */
const hostsOf = (hosts: readonly ConnectedHost[]): Atom.Atom<readonly CostOverviewHost[]> =>
  Atom.make(get => {
    const clock = get(nowMs);
    return hosts.map(({key, label, session}): CostOverviewHost => {
      const view = get(session.model).data.hostCosts.all;
      const feed = get(session.hostCosts.status('all'));
      return {
        key,
        label,
        view,
        feed,
        status: hostStatusOf({
          connection: get(session.connection.state),
          feed,
          hasData: view !== undefined,
          staleForMs: AsyncResult.isSuccess(clock)
            ? staleForMs(get(session.connection.freshness), clock.value)
            : null,
        }),
      };
    });
  });

export function ConnectedCost(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly open: (host: string, runId: string) => void;
}): JSX.Element {
  const {hosts, dispatch} = props;
  useAtomMount(useMemo(() => watchCosts(hosts), [hosts]));
  const rows = useAtomValue(useMemo(() => hostsOf(hosts), [hosts]));
  return (
    <CostOverviewScreen
      hosts={rows}
      words={COST_OVERVIEW_WORDS}
      open={props.open}
      retry={key => {
        const session = hosts.find(candidate => candidate.key === key)?.session;
        if (session !== undefined) dispatch(session.hostCosts.retry('all'), () => undefined);
      }}
    />
  );
}
