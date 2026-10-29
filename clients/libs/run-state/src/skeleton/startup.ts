import {StageState, StartupStage} from '@vibesys/proto';
import type {StoreHistory} from './types.js';

export interface StartupStep {
  readonly stage: 'probing_workspace' | 'building_image' | 'building_tools' | 'starting_container';
  readonly state: 'done' | 'active' | 'waiting';
}
const STAGES: Partial<Record<StartupStage, StartupStep['stage']>> = {
  [StartupStage.PROBING_WORKSPACE]: 'probing_workspace',
  [StartupStage.BUILDING_IMAGE]: 'building_image',
  [StartupStage.BUILDING_TOOLS]: 'building_tools',
  [StartupStage.STARTING_CONTAINER]: 'starting_container',
};
export function startupSteps(history: StoreHistory, seq: bigint): readonly StartupStep[] {
  const steps = new Map<StartupStep['stage'], StartupStep>();
  for (const version of history.lifecycles) {
    if (version.seq > seq) break;
    const state = version.lifecycle?.state;
    if (state?.case !== 'starting') continue;
    const stage = STAGES[state.value.stage];
    if (!stage) continue;
    steps.set(stage, {
      stage,
      state:
        state.value.state === StageState.FINISHED
          ? 'done'
          : state.value.state === StageState.WAITING
            ? 'waiting'
            : 'active',
    });
  }
  return [...steps.values()];
}
