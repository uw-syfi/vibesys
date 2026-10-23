/** The core vocabulary for how a run ended: the schema's `outcome_presentation` option, read once. */
import {create, getOption} from '@bufbuild/protobuf';
import {
  type Lifecycle,
  LifecycleSchema,
  type Outcome,
  OutcomeSchema,
  outcome_presentation,
  type RunOutcomePresentation,
} from './generated/vibesys/store/v1/lifecycle_pb.js';

/** The presentation of an outcome; an outcome the schema does not describe is refused by name. */
export function outcomePresentation(outcome: Outcome): RunOutcomePresentation {
  const value = OutcomeSchema.values.find(item => item.number === outcome);
  const presentation = value ? getOption(value, outcome_presentation) : undefined;
  if (!presentation?.status) throw new Error(`ended.outcome: unknown outcome ${outcome}`);
  return presentation;
}

/**
 * The lifecycle as the writer's head states it: an ended lifecycle carries the presentation of its
 * outcome, whether the records stated it or not. Mirrors `vs_runlog.project_lifecycle`.
 */
export function projectLifecycle(lifecycle: Lifecycle | undefined): Lifecycle | undefined {
  if (lifecycle?.state.case !== 'ended') return lifecycle;
  const ended = lifecycle.state.value;
  return create(LifecycleSchema, {
    ...lifecycle,
    state: {case: 'ended', value: {...ended, presentation: outcomePresentation(ended.outcome)}},
  });
}
