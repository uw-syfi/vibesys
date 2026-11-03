/** Filter a oneof with an explicit decision about every declared kind.
 * The required key set makes adding a generated kind a compiler error at each caller,
 * including callers that deliberately ignore that kind. Undefined bodies never match.
 */
export function oneofIs<T extends {case: string | undefined}, K extends T['case']>(
  body: T | undefined,
  selected: K,
  _kinds: Record<Exclude<NoInfer<T>['case'], undefined>, true>,
): body is Extract<T, {case: K}> {
  return body !== undefined && body.case === selected;
}
