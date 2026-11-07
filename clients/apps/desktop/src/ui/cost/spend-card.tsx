import {Card, Meter, Row, Stack, Stat, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {SpendFigures} from './cost-model.js';
import {NOT_REPORTED} from './words.js';

/** The mark of a cost that is a floor, set close to its figure by a narrow space. */
const FLOOR = '≥\u2005';

/**
 * What a run has cost against its cap and how fast it is spending: the figures of the Cost screen's
 * first card, for any screen that shows a run's cost in brief (`costScreenModel(view, words).spend`).
 */
export function SpendCard(props: {readonly spend: SpendFigures}): JSX.Element {
  const {spend} = props;
  const figure = `${spend.floor ? FLOOR : ''}${spend.figure ?? NOT_REPORTED}`;
  return (
    <Card section="Spend">
      <Stack gap="space-4">
        <Row gap="space-4" align="baseline" wrap>
          <Text variant="hero" tone={spend.figure === null ? 'muted' : 'default'}>
            {figure}
          </Text>
          {spend.cap === null ? null : (
            <Text variant="heading" tone="muted">{`of ${spend.cap}`}</Text>
          )}
        </Row>
        {spend.capShare === null ? null : (
          <Meter label={`${figure} of ${spend.cap ?? ''}`} value={spend.capShare} />
        )}
        <Row gap="space-7" rowGap="space-4" wrap>
          <Stat
            label="Last hour"
            value={spend.lastHour ?? NOT_REPORTED}
            quiet={spend.lastHour === null}
          />
          <Stat label="Turns" value={spend.turns} />
          <Stat label="Unpriced turns" value={spend.unpricedTurns} />
        </Row>
      </Stack>
    </Card>
  );
}
