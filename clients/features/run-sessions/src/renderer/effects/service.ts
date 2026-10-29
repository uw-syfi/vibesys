import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {
  CHAT_CATALOG_METHOD,
  CHAT_SESSIONS_METHOD,
  chatCatalogRequest,
  chatCatalogResponse,
  chatRequest,
  HOST_CALL_METHOD,
  hostChatRequest,
  hostChatResponse,
  sessionCreateRequest,
  sessionCreateResponse,
  sessionListRequest,
  sessionListResponse,
} from '@vibesys/runs/chat';
import {hostIntentRequest, hostIntentResponse, type PostIntentRequest} from '@vibesys/runs/intents';

export interface ChatServicePorts {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly runId: string;
  readonly projectRoot: string;
  readonly schedule: ScheduleTimeout;
}

/** Every single-reply call owns cancellation and a bounded client deadline. */
export function chatService(ports: ChatServicePorts) {
  async function unary(method: string, bytes: Uint8Array, signal: AbortSignal) {
    if (signal.aborted) throw new Error('Cancelled');
    const call = ports.bridge.unary(ports.host, method, bytes);
    let rejectCall: (error: Error) => void = () => undefined;
    const stopped = new Promise<never>((_resolve, reject) => {
      rejectCall = reject;
    });
    const cancel = () => {
      call.cancel();
      rejectCall(new Error('Cancelled'));
    };
    const stopDeadline = ports.schedule(() => {
      call.cancel();
      rejectCall(new Error('Chat operation not confirmed'));
    }, 15000);
    signal.addEventListener('abort', cancel, {once: true});
    try {
      return await Promise.race([call.result, stopped]);
    } finally {
      stopDeadline();
      signal.removeEventListener('abort', cancel);
      call.cancel();
    }
  }
  return {
    control: async (request: PostIntentRequest, signal: AbortSignal) =>
      hostIntentResponse(
        await unary(HOST_CALL_METHOD, hostIntentRequest(request), signal),
        request.requestId,
      ),
    list: async (signal: AbortSignal) =>
      sessionListResponse(
        await unary(CHAT_SESSIONS_METHOD, sessionListRequest(ports.runId), signal),
      ),
    catalog: async (requestId: string, signal: AbortSignal) =>
      chatCatalogResponse(
        await unary(CHAT_CATALOG_METHOD, chatCatalogRequest(requestId), signal),
        requestId,
      ),
    create: async (
      sessionId: string,
      harness: {provider: string; model: string},
      signal: AbortSignal,
    ) =>
      sessionCreateResponse(
        await unary(
          HOST_CALL_METHOD,
          sessionCreateRequest({
            projectRoot: ports.projectRoot,
            runId: ports.runId,
            sessionId,
            ...harness,
          }),
          signal,
        ),
        sessionId,
      ),
    send: async (sessionId: string, requestId: string, text: string, signal: AbortSignal) => {
      const request = chatRequest({
        projectRoot: ports.projectRoot,
        runId: ports.runId,
        sessionId,
        requestId,
        text,
      });
      return hostChatResponse(
        await unary(HOST_CALL_METHOD, hostChatRequest(request), signal),
        request,
      );
    },
  };
}
