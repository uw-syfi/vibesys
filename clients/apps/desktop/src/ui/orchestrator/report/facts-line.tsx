import {Fact, FactLine, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunFacts} from '../../../state/index.js';
import {formatCount, NOT_REPORTED, precisionOf, usdOrNothing} from '../../cost/index.js';

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

const CUT = 'The host served only part of this, so there are more than it shows.';

/**
 * The whole run in four facts: what was measured, the ideas, the agents that are working and what
 * it has cost. Figures are the host's (`RunFacts`), in the text color; the words are quiet. Where
 * the host cut what it served short the fact says so on hover, and where the spend leaves out
 * turns without a price it says that. The age of the newest measurement and the turns behind the
 * spend are the panels' own to say.
 */
export function FactsLine(props: {readonly facts: RunFacts}): JSX.Element {
  const {measured, ideas, agents, spend} = props.facts;
  const money =
    spend === undefined ? null : (usdOrNothing(spend.usd, precisionOf(spend.usd)) ?? NOT_REPORTED);
  return (
    <FactLine spaced>
      {measured.count === 0 ? (
        <Fact>
          <Text variant="small" tone="muted">
            nothing measured yet
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
            no ideas yet
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
      {money === null || spend === undefined ? (
        <Fact>
          <Text variant="small" tone="muted">
            reading cost
          </Text>
        </Fact>
      ) : (
        <Counted
          figure={money}
          words="spent"
          {...(spend.floor
            ? {hint: 'At least this much: the turns that reported no price are not in it.'}
            : {})}
        />
      )}
    </FactLine>
  );
}
