import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  HandoffSchema,
  JobFinishedSchema,
  LeadBriefSchema,
  LeadHandoverSchema,
  type Message,
  MessageSchema,
  ReportDueSchema,
  ReportSchema,
  RunLimit,
  RunLimitReachedSchema,
  SetupBriefSchema,
  SubagentSettledSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {messageIs, noteOf} from './messages.js';

type Kind = Parameters<typeof messageIs>[1];

/** Each kind of body, named, with one example. */
const BODIES: readonly (readonly [Kind, Message['body']])[] = [
  ['text', {case: 'text', value: 'hello'}],
  ['handoff', {case: 'handoff', value: create(HandoffSchema, {claim: 'did it'})}],
  ['report', {case: 'report', value: create(ReportSchema, {headline: 'done'})}],
  ['jobFinished', {case: 'jobFinished', value: create(JobFinishedSchema, {jobId: 'j1'})}],
  [
    'subagentSettled',
    {case: 'subagentSettled', value: create(SubagentSettledSchema, {agentId: 'a'})},
  ],
  ['brief', {case: 'brief', value: 'work'}],
  ['leadBrief', {case: 'leadBrief', value: create(LeadBriefSchema, {task: 'faster'})}],
  [
    'setupBrief',
    {case: 'setupBrief', value: create(SetupBriefSchema, {repository: '/repo', steering: 'tune'})},
  ],
  [
    'handoffReminder',
    {case: 'handoffReminder', value: {$typeName: 'vibesys.orchestrator.v1.HandoffReminder'}},
  ],
  ['leadHandover', {case: 'leadHandover', value: create(LeadHandoverSchema, {task: 'faster'})}],
  ['reportDue', {case: 'reportDue', value: create(ReportDueSchema, {everySeconds: 60})}],
  [
    'runLimitReached',
    {case: 'runLimitReached', value: create(RunLimitReachedSchema, {limit: RunLimit.TIME})},
  ],
];

const messageOf = (body: Message['body']): Message => create(MessageSchema, {id: 'm', body});

describe('a message body', () => {
  it('is of exactly one kind, whichever kind is asked about', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...BODIES),
        fc.constantFrom(...BODIES),
        ([name, body], [asked]) => {
          expect(messageIs(messageOf(body), asked)).toBe(name === asked);
        },
      ),
    );
  });

  it('is never of a kind when there is no message', () => {
    expect(messageIs(undefined, 'text')).toBe(false);
  });

  it('reads as a note of its own kind, a progress report asked for included', () => {
    for (const [, body] of BODIES) expect(noteOf(messageOf(body)).kind).toBeTruthy();
    expect(noteOf(messageOf({case: 'reportDue', value: create(ReportDueSchema)})).kind).toBe(
      'report-due',
    );
    expect(
      noteOf(messageOf({case: 'setupBrief', value: create(SetupBriefSchema, {steering: 'tune'})})),
    ).toEqual({kind: 'brief', text: 'tune'});
    expect(
      noteOf(messageOf({case: 'leadBrief', value: create(LeadBriefSchema, {task: 'x'})})),
    ).toEqual({
      kind: 'brief',
      text: 'x',
    });
  });
});
