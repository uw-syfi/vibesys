import {describe, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {RunSchema, RunStatus} from './generated/vibesys/orchestrator/v1/orchestrator_pb.js';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  decodeValidated,
  encodeMessage,
  Harness,
  RunChangeSchema,
} from './index.js';

describe('the orchestrator bindings', () => {
  test('an event survives the wire', () => {
    const event = create(RunChangeSchema, {
      sequence: 7n,
      runId: 'run-1',
      at: timestampFromMs(1_000),
      body: {
        case: 'run',
        value: create(RunSchema, {
          id: 'run-1',
          repository: 'repo',
          task: 'task',
          status: RunStatus.RUNNING,
          leadAgentId: 'lead',
          createdAt: timestampFromMs(1_000),
        }),
      },
    });
    const decoded = decodeValidated(RunChangeSchema, encodeMessage(RunChangeSchema, event));
    expect(decoded.body.case).toBe('run');
    expect(decoded.sequence).toBe(7n);
  });

  test('a message that breaks a rule is refused with the field named', () => {
    const agent = create(AgentSchema, {
      id: '',
      runId: 'run-1',
      role: AgentRole.LEAD,
      harness: Harness.CODEX,
      status: AgentStatus.ACTIVE,
      stopStage: AgentStopStage.NONE,
      session: 1,
      startedAt: timestampFromMs(0),
    });
    expect(() => decodeValidated(AgentSchema, encodeMessage(AgentSchema, agent))).toThrow(/id/);
  });
});
