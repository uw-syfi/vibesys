import {type Figure, FigureStrip} from '@vibesys/ui';
import type {JSX} from 'react';
import type {FleetOverview} from '../../state/index.js';
import {dollars} from './fleet-words.js';

/**
 * A summary of the live fleet, above the run list, as one band of labelled figures: the live runs,
 * how many of them want the operator, the agents working across them and their spend. A reading of
 * the rows shown, so it answers the same All/Live/Ended filter and search as the list below it. The
 * attention figure is the one thing to act on, so it alone takes the problem tone, and only when it
 * is above zero. A figure the hosts did not report is left out rather than shown as a real zero, and
 * the whole band is hidden when there is nothing live to say.
 */
export function FleetOverviewBar(props: {readonly overview: FleetOverview}): JSX.Element | null {
  const {live, attention, agentsWorking, liveCostUsd} = props.overview;
  if (attention === 0 && agentsWorking === null && liveCostUsd === null) return null;
  const figures: Figure[] = [
    {label: 'Live runs', value: String(live), hint: 'Runs still starting, running or stopping'},
    {
      label: 'Need attention',
      value: String(attention),
      detail: attention === 0 ? 'Nothing is waiting on you' : `of ${live} live`,
      ...(attention > 0 ? {tone: 'warn' as const} : {tone: 'muted' as const}),
      hint: 'Live runs with a stall, stuck jobs, a limit reached, kept checkouts, or an unreadable report',
    },
    ...(agentsWorking === null
      ? []
      : [
          {
            label: 'Agents working',
            value: String(agentsWorking),
            detail: agentsWorking === 1 ? 'agent across live runs' : 'agents across live runs',
          },
        ]),
    ...(liveCostUsd === null
      ? []
      : [
          {
            label: 'Live spend',
            value: dollars(liveCostUsd),
            detail: 'by runs still working',
            hint: 'Priced turns of the live runs shown',
          },
        ]),
  ];
  return <FigureStrip label="Live fleet" figures={figures} />;
}
