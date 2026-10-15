/**
 * A live run's stop controls: Stop, "Stopping…", or the failure with Force stop. Presentational: it
 * renders the run's stop flow and reports the stop asked for.
 */

import type {StopView} from '@vibesys/runs';
import {Button} from '@vibesys/ui';
import type {JSX} from 'react';

export function StopControls(props: {
  readonly view: StopView | undefined;
  readonly stop: (force: boolean) => void;
}): JSX.Element {
  const view = props.view;
  if (view?.phase === 'stopping') return <span className="muted">{view.text}</span>;
  const failed = view?.phase === 'error' ? view : null;
  const confirming = view?.phase === 'confirming';
  return (
    <>
      {failed === null ? null : <span className="error">{failed.text}</span>}
      {failed?.canForce === true ? (
        <Button onClick={() => props.stop(true)}>Force stop</Button>
      ) : null}
      <Button disabled={confirming} onClick={() => props.stop(false)}>
        Stop run
      </Button>
    </>
  );
}
