import {Card, type CheckItem, CheckList} from '@vibesys/ui';
import type {JSX} from 'react';
import {gateFailureView, gateHistoryView, type RunModel} from '../../state/index.js';

/** A row's detail from its parts, or no detail when it has none. */
function detailOf(parts: readonly string[]): {detail?: string} {
  return parts.length === 0 ? {} : {detail: parts.join(' · ')};
}

/**
 * The gates that failed before the run's first idea began, as one failure: which gates, why, and
 * what they printed behind a disclosure. A gate a later attempt passed is no failure: it reads
 * passed, its failed attempts kept as history. Nothing is drawn when none failed.
 */
export function GateFailures(props: {readonly model: RunModel}): JSX.Element | null {
  const failed = gateFailureView(props.model.inputGates);
  const history = gateHistoryView(props.model.inputGates);
  const items: CheckItem[] = [];
  if (failed !== null) {
    items.push({
      id: 'input-gates',
      state: 'fail',
      label: failed.label,
      ...detailOf([
        ...(failed.attempts > 1 ? [`${failed.attempts} attempts`] : []),
        ...(failed.reason === null ? [] : [failed.reason]),
      ]),
      ...(failed.tail === null ? {} : {raw: failed.tail}),
    });
  }
  if (history !== null) {
    items.push({
      id: 'input-gates-history',
      state: 'ok',
      label: history.label,
      ...detailOf([
        `${history.attempts} failed`,
        ...(history.reason === null ? [] : [history.reason]),
      ]),
      ...(history.tail === null ? {} : {raw: history.tail}),
    });
  }
  if (items.length === 0) return null;
  return (
    <Card>
      <CheckList items={items} />
    </Card>
  );
}
