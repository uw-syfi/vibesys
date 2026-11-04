/** Builders for the commands a client sends, so no caller assembles a message by hand. */
import {create} from '@bufbuild/protobuf';
import {type Command, CommandSchema} from './generated/vibesys/orchestrator/v1/events_pb.js';

/** Say `text` to one agent. The id is the idempotency key: a retry with it gets the same receipt. */
export function sendMessageCommand(id: string, agentId: string, text: string): Command {
  return create(CommandSchema, {id, body: {case: 'sendMessage', value: {agentId, text}}});
}
