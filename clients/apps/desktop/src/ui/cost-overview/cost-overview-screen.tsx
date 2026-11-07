import {Page} from '@vibesys/ui';
import type {JSX} from 'react';

/**
 * Cost: the cost of every run together. A placeholder holding the page's place in the rail until
 * the aggregate screen is built; it says only its name. The cost of one run is `ui/cost`.
 */
export function CostOverviewScreen(): JSX.Element {
  return (
    <Page width="wide" state="ready" title="Cost">
      {null}
    </Page>
  );
}
