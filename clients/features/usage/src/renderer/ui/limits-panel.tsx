/**
 * The Limits tab: one card per provider account, a bar per window with the share left and when it
 * refills. Presentational: it renders the folded cards and says only status words and numbers.
 */
import {Button, Card, Chip, LoadingBar, Meter, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {LimitBar, LimitCard, LimitsScreenState} from '../../state/index.js';

/** A window is running out when this share or less of it is left. */
const WARN_LEFT = 0.2;

function Bar(props: {readonly bar: LimitBar; readonly dim: boolean}): JSX.Element {
  const {bar, dim} = props;
  return (
    <Stack gap="space-1">
      <Row justify="between" align="baseline" gap="space-3">
        <Text variant="small">{bar.label}</Text>
        <Row gap="space-3" align="baseline">
          <Text variant="small">{bar.leftText}</Text>
          {bar.resets === null ? null : (
            <Text variant="small" tone="muted">{`Resets in ${bar.resets}`}</Text>
          )}
        </Row>
      </Row>
      <Meter
        label={`${bar.label} ${bar.leftText}`}
        value={bar.left / 100}
        tone={bar.left / 100 <= WARN_LEFT ? 'warn' : 'accent'}
        dim={dim}
      />
    </Stack>
  );
}

function AccountCard(props: {readonly card: LimitCard}): JSX.Element {
  const {card} = props;
  const stale = card.status === 'Stale';
  return (
    <Card>
      <Stack gap="space-3">
        <Row justify="between" align="center" gap="space-3" wrap>
          <Row gap="space-3" align="baseline">
            <Text as="h2" variant="heading">
              {card.providerName}
            </Text>
            {card.plan === null ? null : (
              <Text variant="small" tone="muted">
                {card.plan}
              </Text>
            )}
          </Row>
          <Row gap="space-2" align="center">
            {card.credits > 0 ? <Chip label={`${card.credits} resets`} tone="ok" /> : null}
            {card.status === null ? null : (
              <Chip
                label={card.age === null ? card.status : `${card.status} ${card.age}`}
                tone="warn"
              />
            )}
          </Row>
        </Row>
        {card.bars.map(bar => (
          <Bar key={bar.id} bar={bar} dim={stale} />
        ))}
      </Stack>
    </Card>
  );
}

export function LimitsPanel(props: {
  readonly limits: LimitsScreenState;
  readonly refresh: () => void;
}): JSX.Element {
  const {limits, refresh} = props;
  if (limits.unsupported) {
    return (
      <Text variant="heading" tone="muted">
        Update required
      </Text>
    );
  }
  if (limits.view === null) {
    if (limits.failed) {
      return (
        <Row gap="space-3" align="center">
          <Text variant="heading" tone="muted">
            Unavailable
          </Text>
          <Button text="Retry" variant="secondary" size="sm" press={refresh} />
        </Row>
      );
    }
    return (
      <Stack gap="space-3">
        <LoadingBar label="Reading limits" />
        <Text variant="heading" tone="muted">
          Loading
        </Text>
      </Stack>
    );
  }
  if (limits.view.cards.length === 0) {
    return (
      <Text variant="heading" tone="muted">
        No limits
      </Text>
    );
  }
  return (
    <Stack gap="space-4">
      {limits.checking || limits.failed ? (
        <Text variant="small" tone="muted">
          {limits.checking ? 'Refreshing' : 'Stale'}
        </Text>
      ) : null}
      {limits.view.cards.map(card => (
        <AccountCard key={card.key} card={card} />
      ))}
    </Stack>
  );
}
