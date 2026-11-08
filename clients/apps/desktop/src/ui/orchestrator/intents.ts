/** What the person asked for, as the commands the host takes. */
import {create} from '@bufbuild/protobuf';
import {DurationSchema} from '@bufbuild/protobuf/wkt';
import {
  AgentSpecSchema,
  CostLimitUpdateSchema,
  SendMessageSchema,
  type StartRun,
  StartRunSchema,
  StopRunSchema,
  TimeLimitUpdateSchema,
  type UpdateRun,
  UpdateRunSchema,
} from '@vibesys/orchestrator-proto';
import type {Intent} from '../../core/index.js';
import type {RunUpdateRequest} from './run-settings/update-draft.js';
import type {StartRunRequest} from './start-draft.js';

/** The StartRun message a request becomes. */
export function startRunMessage(request: StartRunRequest): StartRun {
  return create(StartRunSchema, {
    repository: request.repository,
    task: request.task,
    fanoutLimit: request.fanoutLimit,
    lead: create(AgentSpecSchema, {harness: request.lead.harness, model: request.lead.model}),
    brief: request.brief,
    evalPlace: request.evalPlace,
    ...(request.maxTimeSeconds === null
      ? {}
      : {maxTime: create(DurationSchema, {seconds: BigInt(request.maxTimeSeconds)})}),
    ...(request.maxCostUsd === null ? {} : {maxCostUsd: request.maxCostUsd}),
  });
}

export function startIntent(request: StartRunRequest): Intent {
  return {case: 'startRun', value: startRunMessage(request)};
}

export function stopIntent(runId: string): Intent {
  return {case: 'stopRun', value: create(StopRunSchema, {runId})};
}

export function sendIntent(agentId: string, text: string): Intent {
  return {case: 'sendMessage', value: create(SendMessageSchema, {agentId, text})};
}

/** The UpdateRun message a request becomes: each setting it names, and none it does not. */
export function updateRunMessage(runId: string, request: RunUpdateRequest): UpdateRun {
  const {cost, time, fanout, lead} = request;
  return create(UpdateRunSchema, {
    runId,
    ...(cost === undefined
      ? {}
      : {
          costLimit: create(CostLimitUpdateSchema, {
            change:
              'set' in cost ? {case: 'setUsd', value: cost.set} : {case: 'remove', value: true},
          }),
        }),
    ...(time === undefined
      ? {}
      : {
          timeLimit: create(TimeLimitUpdateSchema, {
            change:
              'set' in time
                ? {case: 'set', value: create(DurationSchema, {seconds: BigInt(time.set)})}
                : {case: 'remove', value: true},
          }),
        }),
    ...(fanout === undefined ? {} : {fanoutLimit: fanout}),
    ...(lead === undefined
      ? {}
      : {lead: create(AgentSpecSchema, {harness: lead.harness, model: lead.model})}),
  });
}

export function updateRunIntent(runId: string, request: RunUpdateRequest): Intent {
  return {case: 'updateRun', value: updateRunMessage(runId, request)};
}
