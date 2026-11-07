import {Button, Card, Grid, Row, Stack, Stat, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {AgentsStrip} from '../../../state/index.js';
import {roleWord} from '../words.js';

/**
 * The agents of the run by type: for each type, how many are working now, and of how many the run
 * has had. The lead spawns agents as it likes, so the types shown are the ones that exist. The link
 * opens the Agents screen, which this card knows only as an intent.
 */
export function AgentsCard(props: {
  readonly agents: AgentsStrip;
  readonly open: () => void;
}): JSX.Element {
  const {agents} = props;
  return (
    <Card section="Live agents">
      <Stack gap="space-4">
        {agents.types.length === 0 ? (
          <Text tone="muted">No agents yet</Text>
        ) : (
          <Grid min="tile" gap="space-4">
            {agents.types.map(type => (
              <Stat
                key={type.role}
                label={roleWord(type.role)}
                value={String(type.running)}
                sub={`of ${type.total}`}
              />
            ))}
          </Grid>
        )}
        <Row gap="space-3" justify="between" wrap>
          <Text variant="small" tone="muted">
            {`${agents.running} working · ${agents.total} in all`}
          </Text>
          <Button text="Agents" label="Open agents" variant="quiet" size="sm" press={props.open} />
        </Row>
      </Stack>
    </Card>
  );
}
