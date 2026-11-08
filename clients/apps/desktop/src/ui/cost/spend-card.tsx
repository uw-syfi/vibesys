import {Card, Meter, Row, Stack, Stat, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {SpendFigures} from './cost-model.js';
import {FLOOR, NOT_REPORTED} from './words.js';

/**
 * What a run has cost against its cap and how fast it is spending: the figures of the Cost screen's
 * first card, for any screen that shows a run's cost in brief (`costScreenModel(view, words).spend`).
 * A figure that is a floor says so beside it, with how many turns have no price; the spend rate of
 * the last hour is for a run that is spending, so a run that is over shows none.
 */
export function SpendCard(props: {
  readonly spend: SpendFigures;
  /** The run has ended: it spends nothing now. */
  readonly runOver: boolean;
  /** The link that opens the whole screen, at the end of the card's heading. */
  readonly action?: ReactNode;
  /** The packed card, for a page of dense cards (the run report). */
  readonly dense?: boolean;
}): JSX.Element {
  const {spend} = props;
  const figure = `${spend.floor ? FLOOR : ''}${spend.figure ?? NOT_REPORTED}`;
  return (
    <Card
      section="Spend"
      {...(props.dense === true ? {dense: true} : {})}
      {...(props.action === undefined ? {} : {action: props.action})}
    >
      <Stack gap="space-4">
        <Row gap="space-4" align="baseline" wrap>
          <Text variant="hero" tone={spend.figure === null ? 'muted' : 'default'}>
            {figure}
          </Text>
          {spend.cap === null ? null : (
            <Text variant="heading" tone="muted">{`of ${spend.cap}`}</Text>
          )}
          {spend.floor ? (
            <Text
              variant="small"
              tone="muted"
              title="At least this much: the turns without a price are not in it"
            >{`${spend.unpricedTurns} unpriced`}</Text>
          ) : null}
        </Row>
        {spend.capShare === null ? null : (
          <Meter label={`${figure} of ${spend.cap ?? ''}`} value={spend.capShare} />
        )}
        <Row gap="space-7" rowGap="space-4" wrap>
          {props.runOver ? null : (
            <Stat
              label="Last hour"
              value={spend.lastHour ?? NOT_REPORTED}
              quiet={spend.lastHour === null}
            />
          )}
          {spend.runs === undefined ? null : <Stat label="Runs" value={spend.runs} />}
          <Stat label="Turns" value={spend.turns} />
          {spend.floor ? null : <Stat label="Unpriced turns" value={spend.unpricedTurns} />}
        </Row>
      </Stack>
    </Card>
  );
}
