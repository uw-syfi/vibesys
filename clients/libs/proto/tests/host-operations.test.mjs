import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import * as host from '@vibesys/proto';

const requests = JSON.parse(
  readFileSync(new URL('../../../../proto/testdata/operations/requests.json', import.meta.url)),
);
const routes = new Map(
  [
    [host.operationPolicies.ListTasks.id, host.ListTasksRequestSchema, host.acceptListTasks],
    [
      host.operationPolicies.ListHarnesses.id,
      host.ListHarnessesRequestSchema,
      host.acceptListHarnesses,
    ],
    [
      host.operationPolicies.ListExamples.id,
      host.ListExamplesRequestSchema,
      host.acceptListExamples,
    ],
    [host.operationPolicies.GetTaskGoal.id, host.GetTaskGoalRequestSchema, host.acceptGetTaskGoal],
    [
      host.operationPolicies.DescribeMachine.id,
      host.DescribeMachineRequestSchema,
      host.acceptDescribeMachine,
    ],
    [host.operationPolicies.ListCompute.id, host.ListComputeRequestSchema, host.acceptListCompute],
    [
      host.operationPolicies.ListAgentLogins.id,
      host.ListAgentLoginsRequestSchema,
      host.acceptListAgentLogins,
    ],
    [
      host.operationPolicies.PreflightRun.id,
      host.PreflightRunRequestSchema,
      host.acceptPreflightRun,
    ],
  ].map(([id, schema, accept]) => [id, {schema, accept}]),
);
const validator = createValidator();
for (const sample of requests) {
  test(sample.name, () => {
    const check = () => {
      const route = routes.get(sample.id);
      if (!route) throw new Error('unknown operation');
      const {schema, accept} = route;
      const message = fromBinary(schema, Buffer.from(sample.payload, 'hex'));
      accept(message);
    };
    if (sample.valid) assert.doesNotThrow(check);
    else assert.throws(check);
  });
}

test('accepted bytes survive mutation and calls check correlation', async () => {
  const signal = new AbortController().signal;
  const builder = {requestId: 'request-a', project: '/project'};
  const accepted = host.acceptListTasks(builder);
  builder.project = '/changed';
  const transport = {
    async invoke(id, bytes, totalMs, receivedSignal) {
      assert.equal(receivedSignal, signal);
      assert.equal(id, 10);
      assert.equal(totalMs, 15000);
      assert.equal(fromBinary(host.ListTasksRequestSchema, bytes).project, '/project');
      return toBinary(
        host.ListTasksResponseSchema,
        create(host.ListTasksResponseSchema, {
          requestId: 'request-a',
          tasks: {version: 1n, projectRoot: '/project'},
        }),
      );
    },
  };
  assert.equal((await host.callListTasks(transport, accepted, signal)).requestId, 'request-a');
  const wrong = {
    async invoke() {
      return toBinary(
        host.ListTasksResponseSchema,
        create(host.ListTasksResponseSchema, {requestId: 'wrong', tasks: {version: 1n}}),
      );
    },
  };
  await assert.rejects(host.callListTasks(wrong, accepted, signal), /correlation/);
  await assert.rejects(host.callListTasks(transport, {}, signal), /unaccepted/);
});

test('failure categories are closed', () => {
  for (const category of Object.values(host.FailureCategory).filter(v => typeof v === 'number')) {
    const failure = create(host.OperationFailureSchema, {category, message: 'diagnostic'});
    assert.equal(
      validator.validate(host.OperationFailureSchema, failure).kind === 'valid',
      category !== 0,
    );
  }
  const failure = create(host.OperationFailureSchema, {category: 999, message: 'diagnostic'});
  assert.equal(validator.validate(host.OperationFailureSchema, failure).kind, 'invalid');
});

test('aborted owners cannot start or receive a late call', async () => {
  const controller = new AbortController();
  const accepted = host.acceptListTasks({requestId: 'request-a', project: '/project'});
  let finish;
  const pending = new Promise(resolve => {
    finish = resolve;
  });
  const transport = {
    async invoke() {
      await pending;
      return toBinary(
        host.ListTasksResponseSchema,
        create(host.ListTasksResponseSchema, {requestId: 'request-a', tasks: {version: 1n}}),
      );
    },
  };
  const call = host.callListTasks(transport, accepted, controller.signal);
  controller.abort();
  finish();
  await assert.rejects(call);
  await assert.rejects(host.callListTasks(transport, accepted, controller.signal));
});

test('registry budgets and route identities cannot be changed by consumers', () => {
  assert.equal(Reflect.set(host.operationPolicies.ListTasks, 'id', 999), false);
  assert.equal(Reflect.set(host.operationPolicies.ListTasks, 'totalMs', 0), false);
  assert.equal(host.operationPolicies.ListTasks.id, 10);
  assert.equal(Reflect.set(host.hostCallBudgets, 'relaunch', {}), false);
  assert.equal(Reflect.set(host.hostCallBudgets.relaunch, 'totalMs', 0), false);
});

test('every client budget exceeds its runner cap', () => {
  assert.ok(Object.keys(host.operationPolicies).length > 0);
  assert.ok(Object.keys(host.hostCallBudgets).length > 0);
  for (const [name, policy] of Object.entries(host.operationPolicies)) {
    assert.ok(policy.totalMs > policy.capMs, name);
    assert.equal(policy.totalMs, policy.capMs + policy.graceMs);
  }
  for (const [name, budget] of Object.entries(host.hostCallBudgets)) {
    assert.ok(budget.totalMs > budget.capMs, name);
    assert.equal(budget.totalMs, budget.capMs + budget.graceMs);
  }
});
