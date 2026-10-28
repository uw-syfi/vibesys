import {RailFrame, RailItem} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {Screen, SidebarAction, SidebarState} from '../../state/index.js';

/** What the rail says besides its destinations: the runs that need the user, and the machines' health. */
export interface RailBadges {
  /** How many runs need the user (failed or stalled); none shows no badge. */
  readonly needsYou: number;
  /** The status word of the machine problem to flag on Machines (`problemWord`), or null. */
  readonly machineWarning: string | null;
}

/** Whether `selected` is `screen`, or a screen that belongs to it (a run belongs to Runs). */
function within(selected: Screen, screen: Screen['kind']): boolean {
  return selected.kind === screen || (screen === 'runs' && selected.kind === 'run');
}

/**
 * The app's one window: a narrow rail (New, Runs with a badge for the runs that need the user,
 * Machines with a warning dot, Usage, and Settings at the bottom) beside the selected screen, which
 * `screen` renders. Runs is in the rail only while the app knows of a run (`showRuns`).
 */
export function AppShell(props: {
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly badges: RailBadges;
  /** Whether the rail lists Runs: false while the app knows of no run. */
  readonly showRuns: boolean;
  readonly screen: (screen: Screen) => ReactNode;
}): JSX.Element {
  const {state, dispatch, badges} = props;
  const selected = state.selection;
  const go = (screen: Screen) => () => dispatch({type: 'select', screen});
  return (
    <RailFrame
      label="VibeSys"
      top={
        <>
          <RailItem
            icon="plus"
            label="New"
            hint="New run"
            active={within(selected, 'new-run')}
            press={go({kind: 'new-run'})}
          />
          {props.showRuns ? (
            <RailItem
              icon="grid"
              label="Runs"
              active={within(selected, 'runs')}
              {...(badges.needsYou > 0
                ? {badge: {count: badges.needsYou, label: `${badges.needsYou} need you`}}
                : {})}
              press={go({kind: 'runs'})}
            />
          ) : null}
          <RailItem
            icon="monitor"
            label="Machines"
            active={within(selected, 'machines')}
            {...(badges.machineWarning === null ? {} : {warning: badges.machineWarning})}
            press={go({kind: 'machines'})}
          />
          <RailItem
            icon="usage"
            label="Usage"
            active={within(selected, 'usage')}
            press={go({kind: 'usage'})}
          />
        </>
      }
      bottom={
        <RailItem
          icon="gear"
          label="Settings"
          active={within(selected, 'settings')}
          press={go({kind: 'settings'})}
        />
      }
    >
      {props.screen(selected)}
    </RailFrame>
  );
}
