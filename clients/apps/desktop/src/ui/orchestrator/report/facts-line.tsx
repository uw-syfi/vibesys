import {Fact, FactLine, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunFacts} from '../../../state/index.js';
import {FLOOR, formatCount, NOT_REPORTED, precisionOf, usdOrNothing} from '../../cost/index.js';
import {formatAge} from './format.js';

/** A count in the facts line's emphasis, with what it counts after it. */
function Counted(props: {readonly count: string; readonly what: string}): JSX.Element {
  return (
    <Fact>
      <Text variant="body-strong">{props.count}</Text>{' '}
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

/** `≥ ` when the host cut what it served short, so the count is a floor. */
const floorOf = (atLeast: boolean): string => (atLeast ? FLOOR : '');

/**
 * The whole run in one line: how much was measured and when last, how many ideas, how many agents
 * are working, and what it has cost. Each count is the host's (`RunFacts`); the age of the newest
 * measurement is told against the time the screen was given. A count the host cut short is a floor
 * and says so, as does a cost that leaves unpriced turns out.
 */
export function FactsLine(props: {readonly facts: RunFacts; readonly nowMs: number}): JSX.Element {
  const {facts, nowMs} = props;
  const {measured, ideas, agents, spend} = facts;
  const money =
    spend === undefined
      ? null
      : `${spend.floor ? FLOOR : ''}${usdOrNothing(spend.usd, precisionOf(spend.usd)) ?? NOT_REPORTED}`;
  return (
    <FactLine>
      {measured.count === 0 ? (
        <Fact>
          <Text variant="small" tone="muted">
            nothing measured yet
          </Text>
        </Fact>
      ) : (
        <>
          <Counted
            count={`${floorOf(measured.atLeast)}${formatCount(measured.count)}`}
            what="measured"
          />
          {measured.latestMs === null ? null : (
            <Fact>
              <Text variant="small" tone="muted">
                {`latest ${formatAge(nowMs - measured.latestMs)}`}
              </Text>
            </Fact>
          )}
        </>
      )}
      <Dot />
      {ideas === null || ideas.count === 0 ? (
        <Fact>
          <Text variant="small" tone="muted">
            no ideas yet
          </Text>
        </Fact>
      ) : (
        <Counted count={`${floorOf(ideas.atLeast)}${formatCount(ideas.count)}`} what="ideas" />
      )}
      <Dot />
      <Counted
        count={`${agents.running} of ${agents.total}`}
        what={agents.total === 1 ? 'agent running' : 'agents running'}
      />
      <Dot />
      {spend === undefined || money === null ? (
        <Fact>
          <Text variant="small" tone="muted">
            reading cost
          </Text>
        </Fact>
      ) : (
        <>
          <Counted count={money} what="spent" />
          <Fact>
            <Text variant="small" tone="muted">
              {`· ${formatCount(spend.turns)} turns`}
            </Text>
          </Fact>
          {spend.unpricedTurns === 0 ? null : (
            <Fact>
              <Text variant="small" tone="muted">
                {`· ${formatCount(spend.unpricedTurns)} unpriced`}
              </Text>
            </Fact>
          )}
        </>
      )}
    </FactLine>
  );
}
