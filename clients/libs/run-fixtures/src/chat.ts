import {create} from '@bufbuild/protobuf';
import {ChatRole, type Record, RecordSchema, SteerMode, SteerTargetKind} from '@vibesys/proto';
/** A session survives park/relaunch; streaming and proposals have their own log sequence. */
export function chatReplay(): readonly Record[] {
  return [
    create(RecordSchema, {
      seq: 1n,
      epoch: 1,
      at: {seconds: 1700000000n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'created',
            value: {id: 'default', title: 'Best result', provider: 'codex', model: 'test-model'},
          },
        },
      },
    }),
    create(RecordSchema, {
      seq: 2n,
      epoch: 1,
      at: {seconds: 1700000001n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {
              requestId: 'history',
              role: ChatRole.USER,
              content: {case: 'text', value: 'What improved?'},
            },
          },
        },
      },
    }),
    create(RecordSchema, {
      seq: 3n,
      epoch: 1,
      at: {seconds: 1700000002n},
      body: {
        case: 'chat',
        value: {event: {case: 'token', value: {requestId: 'history', text: 'The queue avoids'}}},
      },
    }),
    create(RecordSchema, {
      seq: 4n,
      epoch: 1,
      at: {seconds: 1700000003n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {
              requestId: 'history',
              role: ChatRole.ASSISTANT,
              content: {case: 'text', value: 'The queue avoids repeated allocation.'},
            },
          },
        },
      },
    }),
    create(RecordSchema, {
      seq: 5n,
      epoch: 1,
      at: {seconds: 1700000004n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'proposedSteer',
            value: {
              id: 'proposal-a',
              requestId: 'history',
              text: 'Compare allocation counts before changing the queue.',
              target: {kind: SteerTargetKind.RUN},
              mode: SteerMode.NEXT_TURN,
            },
          },
        },
      },
    }),
  ];
}
