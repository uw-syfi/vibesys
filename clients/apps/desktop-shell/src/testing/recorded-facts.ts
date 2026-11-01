/**
 * Run records with the facts the Run screen words: an adoption that skipped the checkout, a turn a
 * scripted agent ran, and a run whose log records no project root. For the browser journey
 * (`e2e/recorded-facts.spec.ts`) and the DOM test that holds what each says (`recorded-facts.dom.test.tsx`).
 */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  AdoptionAttrsSchema,
  AgentClient,
  AgentTurnAttrsSchema,
  Change,
  CheckoutStatus,
  EpochStartedSchema,
  RecordSchema,
  RuntimeType,
  Selection,
  SpanStatus,
} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';

type RecordBody = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

function push(fixture: StoreRunFixture, body: RecordBody): StoreRunFixture {
  const records = [...fixture.records];
  records.push(
    create(RecordSchema, {
      seq: (records.at(-1)?.seq ?? 0n) + 1n,
      epoch: records.at(-1)?.epoch ?? 1,
      at: {seconds: 1700000300n + BigInt(records.length)},
      body,
    }),
  );
  return {...fixture, records};
}

export function withFacts(source: StoreRunFixture): StoreRunFixture {
  const adopted = push(source, {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'op:adopt',
        parent: 'idea-0',
        runtimeType: RuntimeType.OPERATION,
        name: 'adopt',
        status: SpanStatus.DONE,
        startedAt: 40,
        endedAt: 41,
        label: {
          kind: 'adoption',
          title: 'Adopt',
          attrs: anyPack(
            AdoptionAttrsSchema,
            create(AdoptionAttrsSchema, {
              selection: Selection.BASELINE,
              checkoutStatus: CheckoutStatus.OPERATOR_CHANGED_CHECKOUT,
            }),
          ),
        },
      },
    },
  });
  return push(adopted, {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'turn-scripted',
        parent: 'idea-0',
        runtimeType: RuntimeType.OPERATION,
        name: 'implementer',
        status: SpanStatus.DONE,
        startedAt: 42,
        endedAt: 43,
        label: {
          kind: 'agent_turn',
          title: 'implementer',
          attrs: anyPack(
            AgentTurnAttrsSchema,
            create(AgentTurnAttrsSchema, {
              agent: 'agent-a',
              role: 'implementer',
              stage: 'implement',
              client: AgentClient.SCRIPTED,
            }),
          ),
        },
      },
    },
  });
}

export function withoutRoot(source: StoreRunFixture): StoreRunFixture {
  const records = source.records.map(record => {
    if (record.body.case !== 'epochStarted') return record;
    const value = create(EpochStartedSchema, {...record.body.value, projectRoot: undefined});
    return create(RecordSchema, {
      seq: record.seq,
      epoch: record.epoch,
      at: record.at,
      body: {case: 'epochStarted', value},
    });
  });
  return {...source, records};
}
