import {Card, CheckList} from '@vibesys/ui';
import type {JSX} from 'react';
import {gateFailureView, type RunModel} from '../../state/index.js';

/**
 * The gates that failed before the run's first idea began, as one failure: which gates, why in a
 * few words, and what they printed behind a disclosure. Nothing is drawn when none failed.
 */
export function GateFailures(props: {readonly model: RunModel}): JSX.Element | null {
  const view = gateFailureView(props.model.inputGates);
  if (view === null) return null;
  return (
    <Card>
      <CheckList
        items={[
          {
            id: 'input-gates',
            state: 'fail',
            label: view.label,
            ...(view.reason === null ? {} : {detail: view.reason}),
            ...(view.tail === null ? {} : {raw: view.tail}),
          },
        ]}
      />
    </Card>
  );
}
