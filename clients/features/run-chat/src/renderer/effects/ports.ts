import type {HarnessChoice, HarnessListing} from '@vibesys/ipc';
import type {AskInput, ChatOutcome, ControlOutcome, DeliverRequest} from '../../state/index.js';

/** What the chat panel asks of the app: the run's session, the host's catalogue, and an id source. */
export interface RunChatPorts {
  /** The host's vetted harnesses, the catalogue New run draws on. */
  harnesses(): Promise<HarnessListing>;
  /** Create a thread on `harness` (null: the run's own provider and model). */
  createThread(
    harness: HarnessChoice | null,
    signal: AbortSignal,
    requestId: string,
  ): Promise<ChatOutcome<{readonly threadId: string}>>;
  /** Put a question to a thread; the answer streams into the run as events. */
  ask(input: AskInput, signal: AbortSignal): Promise<ChatOutcome<unknown>>;
  /** Send one control (a steer or its withdrawal) and say whether the run took it. */
  control(request: DeliverRequest): Promise<ControlOutcome>;
  /** A control id that has not been used in this run. */
  newControlId(): string;
  /** The most UTF-8 bytes a question may hold (the host's cap): an error question is cut to it. */
  readonly maxQuestionBytes: number;
}
