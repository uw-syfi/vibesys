import {BarRows, Button, Card, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CostFeedSnapshot, CostView} from '../../../state/index.js';
import {costScreenModel, SpendCard, TYPE_COLUMNS} from '../../cost/index.js';
import {COST_WORDS} from '../cost-words.js';

/** What went wrong with the cost stream, beside the figures it last delivered, and the way to retry. */
function FeedProblem(props: {
  readonly feed: CostFeedSnapshot;
  readonly retry: () => void;
}): JSX.Element | null {
  const {status} = props.feed;
  if (status._tag !== 'Failed') return null;
  return (
    <Row gap="space-3" wrap>
      <Text tone="warn" variant="small">
        {COST_WORDS.fault(status.fault.kind)}
      </Text>
      {status.fault.retryable ? (
        <Button text="Retry" label="Retry cost" variant="secondary" size="sm" press={props.retry} />
      ) : null}
    </Row>
  );
}

/**
 * What the run has cost, as the host worked it out, in brief: the spend against its cap and the last
 * hour (the Cost screen's own card), and the split by type of agent. Every figure is the host's, said
 * in words by the Cost feature; a price no turn reported is a dash and a floor says so. The cost
 * stream is followed while the report shows; one that cannot be read says so here, beside the figures
 * it last delivered. The link opens the Cost screen.
 */
export function CostCard(props: {
  readonly cost: CostView | undefined;
  readonly feed: CostFeedSnapshot;
  readonly retry: () => void;
  readonly open: () => void;
}): JSX.Element {
  const {cost, feed} = props;
  const model = cost === undefined ? undefined : costScreenModel(cost, COST_WORDS);
  return (
    <Stack gap="space-5">
      {model === undefined ? (
        <Card section="Cost">
          <Stack gap="space-3">
            {feed.status._tag === 'Failed' ? null : <Text tone="muted">Reading cost</Text>}
            <FeedProblem feed={feed} retry={props.retry} />
          </Stack>
        </Card>
      ) : (
        <>
          <SpendCard spend={model.spend} />
          {model.empty ? null : (
            <Card section="By agent type">
              <BarRows
                label="Cost by agent type"
                nameHeader="Type"
                barLabel="Share of the largest cost"
                columns={TYPE_COLUMNS}
                rows={model.types}
              />
            </Card>
          )}
          <FeedProblem feed={feed} retry={props.retry} />
        </>
      )}
      <Row gap="space-3" justify="end">
        <Button text="Details" label="Open cost" variant="quiet" size="sm" press={props.open} />
      </Row>
    </Stack>
  );
}
