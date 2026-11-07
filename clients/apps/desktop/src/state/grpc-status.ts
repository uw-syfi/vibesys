/**
 * What the end of a gRPC call says. The peer ends a call with a status in its trailers; a stream
 * that ends with none, or with something that is not a status, was cut by the link, not answered
 * by the host. The two must stay apart: a status is the host's own word and travels as such (a
 * refusal is not retried), while a cut link is `UNAVAILABLE` (the reconnect policy redials it).
 */

const UNAVAILABLE = 14;

/** The gRPC status of a call that ended with an error: the peer's own, or a lost link's. */
export interface CallStatus {
  readonly code: number;
  readonly message: string;
}

/** A status the peer can send: one to three digits, no sign, no spaces. */
const STATUS = /^\d{1,3}$/;

/**
 * The failure a stream's end is, or undefined for status OK. `status` is the `grpc-status` the
 * peer sent (absent when it sent none) and `detail` its `grpc-message`.
 */
export function callStatus(status: string | undefined, detail: string): CallStatus | undefined {
  if (status !== undefined && STATUS.test(status)) {
    const code = Number(status);
    return code === 0 ? undefined : {code, message: detail === '' ? `gRPC status ${code}` : detail};
  }
  const said = status === undefined ? 'no gRPC status' : `gRPC status ${JSON.stringify(status)}`;
  const tail = detail === '' ? '' : `: ${detail}`;
  return {code: UNAVAILABLE, message: `the stream ended with ${said}${tail}`};
}
