/** Native log ownership is semantic data; display titles are never routing keys. */
import type {LogStream} from '@vibesys/contracts/host-helper';
import {LogGate, StartupStage, type StreamRef} from '@vibesys/proto';

const STAGES = {
  [StartupStage.PROBING_WORKSPACE]: 'probing_workspace',
  [StartupStage.BUILDING_IMAGE]: 'building_image',
  [StartupStage.BUILDING_TOOLS]: 'building_tools',
  [StartupStage.STARTING_CONTAINER]: 'starting_container',
} as const;
const GATES = {
  [LogGate.BUILD]: 'build',
  [LogGate.ACCURACY]: 'accuracy',
  [LogGate.BENCHMARK]: 'benchmark',
  [LogGate.VALIDATION]: 'validation',
} as const;

export function streamOwner(stream: StreamRef): LogStream['owner'] {
  const span = {span_id: stream.span ?? null};
  const owner = stream.owner?.kind;
  switch (owner?.case) {
    case 'startup': {
      const stage = STAGES[owner.value as keyof typeof STAGES];
      if (!stage) throw new Error(`stream ${stream.id}: unknown startup stage`);
      return {...span, kind: 'startup_step', stage};
    }
    case 'gate': {
      const gate = GATES[owner.value.gate as keyof typeof GATES];
      if (!gate) throw new Error(`stream ${stream.id}: unknown gate`);
      return {
        ...span,
        kind: 'gate_stage',
        execution_id: owner.value.execution,
        gate,
        attempt: owner.value.attempt,
      };
    }
    case 'toolBuild':
      return {...span, kind: 'tool_build', name: owner.value};
    case 'narration':
      return {...span, kind: 'run_narration'};
    default:
      if (stream.owner !== undefined) throw new Error(`stream ${stream.id}: unknown log owner`);
      // Historical references carry no semantic owner. Keep them explicitly unattributed.
      return {...span, kind: 'run'};
  }
}
