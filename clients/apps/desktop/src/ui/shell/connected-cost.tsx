/**
 * The Cost page bound to the hosts' services: while it shows, every host's cost stream for the whole
 * of its history is followed (leaving the page closes them), and each host's served view, status and
 * feed are read from its session. The page computes nothing; a run row opens that run's report.
 */
import {useAtomMount, useAtomValue} from '@effect/atom-react';
import * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo} from 'react';
import {hostStatusOf, staleForMs, workingByRole} from '../../state/index.js';
import {ms} from '../../state/orchestrator/time.js';
import {type CostOverviewHost, CostOverviewScreen} from '../cost-overview/index.js';
import {useDisplayClock} from '../display-clock.js';
import {nowMs} from '../orchestrator/clock.js';
import type {ConnectedHost, Dispatch} from '../orchestrator/connected.js';
import {costOverviewWords} from '../orchestrator/cost-words.js';
import {runTitle} from '../orchestrator/fleet-words.js';

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
      const data = get(session.model).data;
      const view = data.hostCosts.all;
      const feed = get(session.hostCosts.status('all'));
      return {
        key,
        label,
        view,
        titles: Object.fromEntries(
          Object.values(data.runs).map(run => [run.id, runTitle(run.taskCard ?? null, run.task)]),
        ),
        started: Object.fromEntries(
          Object.values(data.runs).flatMap(run => {
            const startedMs = ms(run.createdAt);
            return startedMs === null ? [] : [[run.id, startedMs] as const];
          }),
        ),
        working: workingByRole(data.summaries),
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
  const clock = useDisplayClock();
  const words = useMemo(() => costOverviewWords(clock), [clock]);
  return (
    <CostOverviewScreen
      hosts={rows}
      words={words}
      open={props.open}
      retry={key => {
        const session = hosts.find(candidate => candidate.key === key)?.session;
        if (session !== undefined) dispatch(session.hostCosts.retry('all'), () => undefined);
      }}
    />
  );
}
