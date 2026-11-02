/**
 * The orchestrator's contract as TypeScript: the messages, enums and service descriptor generated
 * from `proto/vibesys/orchestrator/v1` (never edited by hand; regenerate with
 * `buf generate --template buf.gen.orchestrator-ts.yaml`), and the one validated decoder.
 *
 * The orchestrator's protos are not part of the bindings of `@vibesys/proto`, so the existing
 * clients generate what they always did.
 */

export {sendMessageCommand} from './commands.js';
export * from './generated/vibesys/orchestrator/v1/events_pb.js';
export * from './generated/vibesys/orchestrator/v1/orchestrator_pb.js';
export * from './generated/vibesys/orchestrator/v1/service_pb.js';
export {decodeValidated, decodeValidatedJson, encodeJson, encodeMessage} from './validated.js';
