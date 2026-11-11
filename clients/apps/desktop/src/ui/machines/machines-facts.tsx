import {Fact, FactLine, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {MachinesFacts} from '../../state/index.js';

/** A count in the facts line's emphasis, with what it counts after it. */
function Counted(props: {readonly count: number; readonly what: string}): JSX.Element {
  return (
    <Fact>
      <Text variant="body-strong">{String(props.count)}</Text>{' '}
      <Text variant="small" tone="muted">
        {props.what}
      </Text>
    </Fact>
  );
}

const Dot = (): JSX.Element => (
  <Fact>
    <Text variant="small" tone="muted">
      ·
    </Text>
  </Fact>
);

const plural = (count: number, one: string, many: string): string => (count === 1 ? one : many);

/**
 * The page in one line: how many machines, how many are live, how many places jobs can run at and how
 * many of those could not be reached. Every count is the app's (`MachinesFacts`); a count that would
 * be zero says nothing, and places that are not read yet say so instead of being counted as none.
 */
export function MachinesFactsLine(props: {readonly facts: MachinesFacts}): JSX.Element {
  const {machines, live, places, unreachable, unread} = props.facts;
  return (
    <FactLine>
      <Counted count={machines} what={plural(machines, 'machine', 'machines')} />
      <Dot />
      <Counted count={live} what="live" />
      {places === null ? null : (
        <>
          <Dot />
          <Counted count={places} what={plural(places, 'job place', 'job places')} />
        </>
      )}
      {unreachable === 0 ? null : (
        <>
          <Dot />
          <Counted count={unreachable} what="unreachable" />
        </>
      )}
      {unread === 0 ? null : (
        <>
          <Dot />
          <Fact>
            <Text variant="small" tone="muted">
              {`places not read on ${unread}`}
            </Text>
          </Fact>
        </>
      )}
    </FactLine>
  );
}
