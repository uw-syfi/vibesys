/**
 * What the run does with a steer, as the gateway shows it: the page posts a PostIntent, the test
 * (standing in for the run) answers the admission, then appends the run-log records the run would
 * write: an IntentState per outcome, and a `steer` link on the turn that received the steer.
 */
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {anyUnpack} from '@bufbuild/protobuf/wkt';
import type {Page} from '@playwright/test';
import {
  AgentTurnAttrsSchema,
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  type PostIntentRequest,
  RecordSchema,
  SpanLinkSchema,
  SpanSchema,
  SteerMode,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import type {RunFixture} from '@vibesys/run-fixtures';
import {intentReplay} from '@vibesys/run-fixtures';
import type {Refusal} from './chat-peer.js';
import {nativeRecords} from './native-fixture.js';

/** One step of a steer's life, as the old journeys named them. */
export type Step =
  | {readonly state: 'queued'}
  | {readonly state: 'reached' | 'in_force'; readonly role: string}
  | {readonly state: 'dropped'}
  | {readonly state: 'ended'; readonly reason?: 'withdrawn'}
  | {readonly state: 'refused'; readonly reason: string};

interface GatewayWindow {
  fakeGateway: {append(bytes: number[]): void};
}

export class Controls {
  readonly posted: PostIntentRequest[] = [];
  #sequence: bigint;
  readonly #records: readonly StoreRecord[];

  constructor(
    private readonly page: Page,
    fixture: RunFixture,
  ) {
    this.#records = nativeRecords(fixture);
    this.#sequence = this.#records.at(-1)?.seq ?? 0n;
  }

  /** Answer every PostIntent the page sends with its admission, and keep what it asked. */
  async attach(): Promise<void> {
    await this.page.exposeFunction('postIntent', (_host: string, bytes: number[]) => {
      const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
      if (envelope.method.case !== 'postIntent') throw new Error('Expected PostIntent');
      this.posted.push(envelope.method.value);
      return [
        ...toBinary(
          HostCallResponseSchema,
          intentReplay('pending', envelope.method.value.requestId).admission,
        ),
      ];
    });
  }

  /** From now on the host refuses that chat call (until called with `undefined`). */
  async refuse(refusal: Refusal | undefined): Promise<void> {
    await this.page.evaluate(
      payload =>
        (
          window as unknown as {fakeGateway: {refuseChat(refusal: Refusal | undefined): void}}
        ).fakeGateway.refuseChat(payload),
      refusal,
    );
  }

  /** The control id of the newest post. */
  lastId(): string {
    return this.posted.at(-1)?.requestId ?? '';
  }

  /** Append what the run records for `controlId` at this step. */
  async deliver(controlId: string, step: Step): Promise<void> {
    const posted = this.posted.find(request => request.requestId === controlId);
    const mode = posted?.intent.case === 'steer' ? posted.intent.value.mode : SteerMode.NEXT_TURN;
    const records: StoreRecord[] = [];
    const next = () => ++this.#sequence;
    const intent = (
      id: string,
      fields: {
        step: IntentStep;
        action?: IntentAction;
        reason?: string;
        reasonCode?: string;
        withdraws?: string;
      },
    ) =>
      create(RecordSchema, {
        seq: next(),
        epoch: 1,
        at: {seconds: 1700000200n},
        body: {
          case: 'intent',
          value: {controlId: id, action: IntentAction.STEER, mode, ...fields},
        },
      });
    if (step.state === 'queued') records.push(intent(controlId, {step: IntentStep.APPLIED}));
    else if (step.state === 'refused')
      records.push(intent(controlId, {step: IntentStep.REFUSED, reason: step.reason}));
    else if (step.state === 'dropped')
      records.push(
        intent(controlId, {step: IntentStep.REFUSED, reason: 'dropped', reasonCode: 'dropped'}),
      );
    else if (step.state === 'ended' && step.reason === 'withdrawn') {
      const withdrawal = this.posted.find(
        request =>
          request.intent.case === 'withdraw' && request.intent.value.withdraws === controlId,
      );
      records.push(
        intent(withdrawal?.requestId ?? `${controlId}-withdraw`, {
          step: IntentStep.APPLIED,
          action: IntentAction.WITHDRAW,
          withdraws: controlId,
        }),
      );
    } else if (step.state === 'ended')
      records.push(intent(controlId, {step: IntentStep.APPLIED, reasonCode: 'ended'}));
    else records.push(this.#linked(controlId, step.role, next()));
    await this.#append(records);
  }

  /**
   * The run applies the newest posted control: it writes the IntentState that settles it, after
   * the record numbered `after` when the run has since moved on past the fixture's records.
   */
  async settle(action: IntentAction, after?: bigint): Promise<void> {
    if (after !== undefined) this.#sequence = after;
    const record = create(RecordSchema, {
      seq: ++this.#sequence,
      epoch: 1,
      at: {seconds: 1700000200n},
      body: {
        case: 'intent',
        value: {controlId: this.lastId(), action, step: IntentStep.APPLIED},
      },
    });
    await this.#append([record]);
  }

  /** The newest turn span of that role, written again with a steer link to the control. */
  #linked(controlId: string, role: string, seq: bigint): StoreRecord {
    const turn = [...this.#records].reverse().find(record => {
      const label = record.body.case === 'span' ? record.body.value.span?.label : undefined;
      return (
        label?.attrs !== undefined && anyUnpack(label.attrs, AgentTurnAttrsSchema)?.role === role
      );
    });
    if (turn?.body.case !== 'span' || turn.body.value.span?.label === undefined)
      throw new Error(`the fixture has no ${role} turn to receive a steer`);
    const span = clone(SpanSchema, turn.body.value.span);
    span.label?.links.push(create(SpanLinkSchema, {rel: 'steer', to: controlId}));
    return create(RecordSchema, {
      seq,
      epoch: turn.epoch,
      at: {seconds: 1700000200n},
      body: {case: 'span', value: {...turn.body.value, span}},
    });
  }

  async #append(records: readonly StoreRecord[]): Promise<void> {
    const bytes = [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          records: records.map(record => toBinary(RecordSchema, record)),
          throughSeq: records.at(-1)?.seq ?? 0n,
        }),
      ),
    ];
    await this.page.evaluate(
      payload => (window as unknown as GatewayWindow).fakeGateway.append(payload),
      bytes,
    );
  }
}
