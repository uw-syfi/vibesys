/** A fresh identity for one logical request, unique across connections and app restarts.
 * Keep this value when retrying that same request; a new action needs a new identity.
 */
export function newRequestId(): string {
  return globalThis.crypto.randomUUID();
}
