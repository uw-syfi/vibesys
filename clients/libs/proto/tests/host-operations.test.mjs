import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {fromBinary, create, toBinary} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import * as host from '@vibesys/proto';

const requests = JSON.parse(readFileSync(new URL('../../../../proto/testdata/operations/requests.json', import.meta.url)));
const names = new Map(Object.entries(host.operationPolicies).map(([name,policy]) => [policy.id,name]));
const validator = createValidator();
for (const sample of requests) {
  test(sample.name, () => {
    const check = () => {
      const name = names.get(sample.id);
      if (!name) throw new Error('unknown operation');
      const schema = host[`${name}RequestSchema`];
      const message = fromBinary(schema, Buffer.from(sample.payload,'hex'));
      host[`accept${name}`](message);
    };
    if (sample.valid) assert.doesNotThrow(check);
    else assert.throws(check);
  });
}

test('accepted bytes survive mutation and calls check correlation', async () => {
  const signal = new AbortController().signal;
  const builder = {requestId:'request-a',project:'/project'};
  const accepted = host.acceptListTasks(builder);
  builder.project = '/changed';
  const transport = {async invoke(id,bytes,totalMs, receivedSignal) {
    assert.equal(receivedSignal,signal);
    assert.equal(id,10);assert.equal(totalMs,15000);
    assert.equal(fromBinary(host.ListTasksRequestSchema,bytes).project,'/project');
    return toBinary(host.ListTasksResponseSchema,create(host.ListTasksResponseSchema,{requestId:'request-a',tasks:{version:1n,projectRoot:'/project'}}));
  }};
  assert.equal((await host.callListTasks(transport,accepted,signal)).requestId,'request-a');
  const wrong = {async invoke() {return toBinary(host.ListTasksResponseSchema,create(host.ListTasksResponseSchema,{requestId:'wrong',tasks:{version:1n}}));}};
  await assert.rejects(host.callListTasks(wrong,accepted,signal),/correlation/);
  await assert.rejects(host.callListTasks(transport,{},signal),/unaccepted/);
});

test('failure categories are closed', () => {
  for (const category of Object.values(host.FailureCategory).filter(v=>typeof v==='number')) {
    const failure = create(host.OperationFailureSchema,{category,message:'diagnostic'});
    assert.equal(validator.validate(host.OperationFailureSchema,failure).kind === 'valid',category !== 0);
  }
  const failure = create(host.OperationFailureSchema,{category:999,message:'diagnostic'});
  assert.equal(validator.validate(host.OperationFailureSchema,failure).kind,'invalid');
});


test('aborted owners cannot start or receive a late call', async () => {
  const controller = new AbortController();
  const accepted = host.acceptListTasks({requestId:'request-a',project:'/project'});
  let finish;
  const pending = new Promise(resolve => {finish = resolve;});
  const transport = {async invoke() {await pending;return toBinary(host.ListTasksResponseSchema,create(host.ListTasksResponseSchema,{requestId:'request-a',tasks:{version:1n}}));}};
  const call = host.callListTasks(transport,accepted,controller.signal);
  controller.abort();
  finish();
  await assert.rejects(call);
  await assert.rejects(host.callListTasks(transport,accepted,controller.signal));
});
