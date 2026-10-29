import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  CHAT_BLOBS_METHOD,
  CHAT_TAIL_METHOD,
  chatBlobHashes,
  chatBlobRequest,
  chatBlobResponse,
  chatTailRequest,
  chatTailResponse,
  HOST_CALL_METHOD,
  hostChatRequest,
  hostChatResponse,
} from '@vibesys/runs/chat';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type ChatEvent, type ChatHistory, runSessionsMachine} from '../../state/index.js';

export interface SessionPorts {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly runId: string;
  readonly sessionId: string;
  readonly enabled: boolean;
  readonly history?: ChatHistory;
  readonly schedule: ScheduleTimeout;
}
export function sessionsActor(input: SessionPorts) {
  return createActor(
    runSessionsMachine.provide({
      actors: {
        post: fromPromise(async ({input: request, signal}) => {
          const call = input.bridge.unary(input.host, HOST_CALL_METHOD, hostChatRequest(request));
          const cancel = () => call.cancel();
          signal.addEventListener('abort', cancel, {once: true});
          try {
            if (signal.aborted) throw new Error('Cancelled');
            return hostChatResponse(await call.result, request);
          } finally {
            signal.removeEventListener('abort', cancel);
            call.cancel();
          }
        }),
        watch: fromCallback<ChatEvent, bigint>(({input: fromSeq, sendBack}) => {
          const call = input.bridge.stream(
            input.host,
            CHAT_TAIL_METHOD,
            chatTailRequest(input.runId, input.sessionId, fromSeq),
            bytes => {
              try {
                sendBack({type: 'records', records: chatTailResponse(bytes)});
              } catch {
                sendBack({type: 'disconnected'});
              }
            },
            () => sendBack({type: 'disconnected'}),
          );
          return () => call.cancel();
        }),
        resolve: fromPromise(async ({input: records, signal}) => {
          const hashes = chatBlobHashes(records);
          if (hashes.length === 0) return {records, blobs: new Map<string, string>()};
          const blobs = new Map<string, string>();
          for (const hash of hashes) {
            const call = input.bridge.unary(
              input.host,
              CHAT_BLOBS_METHOD,
              chatBlobRequest(input.runId, input.sessionId, [hash]),
            );
            const cancel = () => call.cancel();
            signal.addEventListener('abort', cancel, {once: true});
            try {
              if (signal.aborted) throw new Error('Cancelled');
              for (const [key, value] of chatBlobResponse(await call.result)) blobs.set(key, value);
            } finally {
              signal.removeEventListener('abort', cancel);
              call.cancel();
            }
          }
          return {records, blobs};
        }),
      },
    }),
    {
      input: {enabled: input.enabled, ...(input.history ? {history: input.history} : {})},
      clock: clockFrom(input.schedule),
    },
  );
}
