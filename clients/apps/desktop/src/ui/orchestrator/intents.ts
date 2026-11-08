/** What the person asked for, as the commands the host takes. */
import {create} from '@bufbuild/protobuf';
import {DurationSchema} from '@bufbuild/protobuf/wkt';
import {
  AgentSpecSchema,
  SendMessageSchema,
  type StartRun,
  StartRunSchema,
  StopRunSchema,
} from '@vibesys/orchestrator-proto';
import type {Intent} from '../../core/index.js';
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
