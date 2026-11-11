import {Fact, FactLine, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunFacts} from '../../../state/index.js';
import {
  FLOOR_HINT,
  floored,
  formatCount,
  NOT_REPORTED,
  precisionOf,
  usdOrNothing,
} from '../../cost/index.js';
import {UNKNOWN} from '../../plain-words.js';
import {yet} from './yet.js';

/**
 * One fact: its figure in the text color and its words muted, in one run of text so the space between
 * them is a real space (a fact is a flex box, which drops a space between its children and leaves a
 * 2 px gap). With a `hint` the pointer says more.
 */
function Counted(props: {
  readonly figure: string;
  readonly words: string;
  readonly hint?: string;
}): JSX.Element {
  return (
    <Fact {...(props.hint === undefined ? {} : {title: props.hint})}>
      <Text variant="small" tone="muted">
        <Text variant="small">{props.figure}</Text>
        {` ${props.words}`}
      </Text>
    </Fact>
  );
}

/**
 * The spend as the Cost page writes it: a figure with `≥` while turns without a price are behind it (and
 * what that means on hover), `unknown` when turns ran and none was priced, `—` only before any turn.
 */
function spendWords(spend: NonNullable<RunFacts['spend']>): {
  readonly figure: string;
  readonly hint: string | undefined;
} {
  const usd = usdOrNothing(spend.usd, precisionOf(spend.usd));
  if (usd === null) {
    return spend.turns === 0
      ? {figure: NOT_REPORTED, hint: undefined}
      : {figure: UNKNOWN, hint: 'No turn reported a price.'};
  }
  return spend.floor ? {figure: floored(usd), hint: FLOOR_HINT} : {figure: usd, hint: undefined};
}

const CUT = 'The host served only part of this, so there are more than it shows.';

/**
 * The whole run in four facts: what was measured, the ideas, the agents that are working and what
 * it has cost. Figures are the host's (`RunFacts`), in the text color; the words are quiet. Where
 * the host cut what it served short the fact says so on hover, and where the spend leaves out
 * turns without a price it says that. The age of the newest measurement and the turns behind the
 * spend are the panels' own to say.
 */
export function FactsLine(props: {readonly facts: RunFacts; readonly over: boolean}): JSX.Element {
  const {measured, ideas, agents, spend} = props.facts;
  const {over} = props;
  const money = spend === undefined ? null : spendWords(spend);
  return (
    <FactLine spaced>
      {measured.count === 0 ? (
        <Fact>
          <Text variant="small" tone="muted">
            {yet('nothing measured', over)}
          </Text>
        </Fact>
      ) : (
        <Counted
          figure={formatCount(measured.count)}
          words="measured"
          {...(measured.atLeast ? {hint: CUT} : {})}
        />
      )}
      {ideas === null || ideas.count === 0 ? (
        <Fact>
          <Text variant="small" tone="muted">
            {yet('no ideas', over)}
          </Text>
        </Fact>
      ) : (
        <Counted
          figure={formatCount(ideas.count)}
          words="ideas"
          {...(ideas.atLeast ? {hint: CUT} : {})}
        />
      )}
      <Counted
        figure={`${agents.running} of ${agents.total}`}
        words={agents.total === 1 ? 'agent running' : 'agents running'}
      />
      {money === null ? (
        <Fact>
          <Text variant="small" tone="muted">
            reading cost
          </Text>
        </Fact>
      ) : (
        <Counted
          figure={money.figure}
          words="spent"
          {...(money.hint === undefined ? {} : {hint: money.hint})}
        />
      )}
    </FactLine>
  );
}
