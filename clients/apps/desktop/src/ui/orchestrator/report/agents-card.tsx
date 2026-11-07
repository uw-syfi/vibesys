import {Button, Card, Grid, Stack, Stat, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {AgentsStrip} from '../../../state/index.js';
import {roleWord} from '../words.js';

/**
 * The agents of the run by type: for each type, how many are working now, and of how many the run
 * has had. The lead spawns agents as it likes, so the types shown are the ones that exist. A run
 * that is over has nobody working, so its card is the agents it had, not "live" ones. The link in
 * the heading opens the Agents screen, which this card knows only as an intent.
 */
export function AgentsCard(props: {
  readonly agents: AgentsStrip;
  /** The run has ended: nobody is working, so the card says what the run had. */
  readonly runOver: boolean;
  readonly open: () => void;
}): JSX.Element {
  const {agents} = props;
  return (
    <Card
      section={props.runOver ? 'Agents' : 'Live agents'}
      action={
        <Button text="Agents" label="Open agents" variant="quiet" size="sm" press={props.open} />
      }
    >
      <Stack gap="space-4">
        {agents.types.length === 0 ? (
          <Text tone="muted">No agents yet</Text>
        ) : (
          <Grid min="tile" gap="space-4">
            {agents.types.map(type => (
              <Stat
                key={type.role}
                label={roleWord(type.role)}
                value={props.runOver ? String(type.total) : String(type.running)}
                {...(props.runOver ? {} : {sub: `of ${type.total}`})}
              />
            ))}
          </Grid>
        )}
        <Text variant="small" tone="muted">
          {props.runOver
            ? `${agents.total} in all`
            : `${agents.running} working · ${agents.total} in all`}
        </Text>
      </Stack>
    </Card>
  );
}
