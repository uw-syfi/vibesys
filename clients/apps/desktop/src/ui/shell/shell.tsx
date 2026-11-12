import {RailFrame, RailItem} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {Destination, NavigationAction} from '../../state/index.js';

/**
 * The window: a narrow rail of icons down the left edge (the app's mark, a static picture set apart, then one button
 * for each destination, Settings last, at the rail's foot) and the destination's screen beside it. The rail's buttons send the intents
 * the navigation reducer takes; pressing the destination already shown goes back to its start.
 */
export function AppShell(props: {
  readonly destination: Destination;
  readonly go: (action: NavigationAction) => void;
  readonly children: ReactNode;
}): JSX.Element {
  const {destination, go} = props;
  return (
    <RailFrame
      label="Main"
      state="ready"
      mark={{icon: 'mark', label: 'VibeSys'}}
      top={
        <>
          <RailItem
            icon="list"
            label="Runs"
            active={destination === 'runs'}
            press={() => go({type: 'open-runs'})}
          />
          <RailItem
            icon="usage"
            label="Cost"
            active={destination === 'cost'}
            press={() => go({type: 'open-cost'})}
          />
          <RailItem
            icon="monitor"
            label="Machines"
            active={destination === 'machines'}
            press={() => go({type: 'open-machines'})}
          />
        </>
      }
      bottom={
        <RailItem
          icon="sliders"
          label="Settings"
          active={destination === 'settings'}
          press={() => go({type: 'open-settings'})}
        />
      }
    >
      {props.children}
    </RailFrame>
  );
}
