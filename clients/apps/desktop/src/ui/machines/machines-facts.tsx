import {CountedFact, FactLine} from '@vibesys/ui';
import type {JSX} from 'react';
import type {MachinesFacts} from '../../state/index.js';
import {plural} from '../plain-words.js';

/**
 * The page in one line: how many machines, how many are live, how many places jobs can run at and how
 * many of those could not be reached. Every count is the app's (`MachinesFacts`); a count that would
 * be zero says nothing, and places that are not read yet say so instead of being counted as none.
 */
export function MachinesFactsLine(props: {readonly facts: MachinesFacts}): JSX.Element {
  const {machines, live, places, unreachable, unread} = props.facts;
  return (
    <FactLine spaced>
      <CountedFact figure={String(machines)} words={plural(machines, 'machine', 'machines')} />
      <CountedFact figure={String(live)} words="live" />
      {places === null ? null : (
        <CountedFact figure={String(places)} words={plural(places, 'job place', 'job places')} />
      )}
      {unreachable === 0 ? null : (
        <CountedFact
          figure={String(unreachable)}
          words={plural(unreachable, 'place unreachable', 'places unreachable')}
        />
      )}
      {unread === 0 ? null : (
        <CountedFact
          lead="job places not read on"
          figure={String(unread)}
          words={plural(unread, 'machine', 'machines')}
        />
      )}
    </FactLine>
  );
}
