import {RailFrame, RailItem} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {Destination, NavigationAction} from '../../state/index.js';

/**
 * The window: a narrow rail of icons down the left edge (the app's mark, then one button for each
 * destination) and the destination's screen beside it. The rail's buttons send the intents the
 * navigation reducer takes; pressing the destination already shown goes back to its start.
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
      top={
        <>
          <RailItem icon="mark" label="VibeSys" hideLabel press={() => go({type: 'open-runs'})} />
          <RailItem
            icon="grid"
            label="Runs"
            hideLabel
            active={destination === 'runs'}
            press={() => go({type: 'open-runs'})}
          />
          <RailItem
            icon="gear"
            label="Settings"
            hideLabel
            active={destination === 'settings'}
            press={() => go({type: 'open-settings'})}
          />
        </>
      }
    >
      {props.children}
    </RailFrame>
  );
}
