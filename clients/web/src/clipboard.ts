/** Writes to the system clipboard; `false` if the browser refuses (permissions, insecure context,
 * no `navigator.clipboard`) instead of an unhandled rejection. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
