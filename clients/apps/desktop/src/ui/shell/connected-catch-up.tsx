/**
 * The catch-up page bound to a host's services: this viewer's check-ins in the open run, read from
 * the folded model, and the press of Catch me up, which is a command of the host's session. The
 * viewer is the install this app is (`core/viewer`); a check-in is this viewer's by the id the host
 * stored in it, and the page keeps no list of its own.
 */
import {useAtomValue} from '@effect/atom-react';
import {Page, Text} from '@vibesys/ui';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo} from 'react';
import {type Route, type RouteAction, rewind} from '../../state/index.js';
import {type CatchUpPageWords, CatchUpScreen} from '../catch-up/index.js';
import type {ConnectedHost, Dispatch} from '../orchestrator/connected.js';

type RunRoute = Extract<Route, {readonly screen: 'run'}>;

export function ConnectedCatchUp(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly route: RunRoute;
  readonly go: (action: RouteAction) => void;
  readonly dispatch: Dispatch;
  /** The id of this install, as the host knows it; null while it is not known. */
  readonly viewerId: string | null;
  readonly words: CatchUpPageWords;
}): JSX.Element {
  const {hosts, route, go, dispatch, viewerId, words} = props;
  const host = hosts.find(candidate => candidate.key === route.host);
  const {runId} = route;
  const view = useAtomValue(
    useMemo(
      () =>
        Atom.make(get =>
          host === undefined
            ? null
            : {
                state: get(host.session.model).data,
                press: get(host.session.catchUp.standing(runId)),
              },
        ),
      [host, runId],
    ),
  );
  const run = view?.state.runs[runId];
  if (host === undefined || view === null || run === undefined) {
    return (
      <Page key="catch-up" width="wide" state="loading" title="Catch me up">
        <Text tone="muted">Connecting.</Text>
      </Page>
    );
  }
  const {session} = host;
  return (
    <CatchUpScreen
      run={run.task}
      rows={viewerId === null ? [] : rewind(view.state, runId, viewerId)}
      press={view.press}
      catchUp={() => dispatch(session.catchUp.press(runId), () => undefined)}
      goRuns={() => go({type: 'close-run'})}
      goRun={() => go({type: 'show', view: 'report'})}
      words={words}
    />
  );
}
