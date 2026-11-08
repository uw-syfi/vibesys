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
  ProvisionRepositoryRequestSchema,
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

  test('a repository is made under an absolute folder, the home folder, or the host default', () => {
    const request = (parentDirectory: string) =>
      create(ProvisionRepositoryRequestSchema, {
        requestId: 'r',
        name: 'repo',
        source: {case: 'empty', value: {}},
        parentDirectory,
      });
    const parses = (parent: string): boolean => {
      try {
        decodeValidated(
          ProvisionRepositoryRequestSchema,
          encodeMessage(ProvisionRepositoryRequestSchema, request(parent)),
        );
        return true;
      } catch {
        return false;
      }
    };
    for (const allowed of ['', '/srv/repos', '~', '~/work', '~/a/b'])
      expect(parses(allowed)).toBe(true);
    for (const refused of ['work', '~user/x', '~work', './x', '~/a\0b'])
      expect(parses(refused)).toBe(false);
  });
});
