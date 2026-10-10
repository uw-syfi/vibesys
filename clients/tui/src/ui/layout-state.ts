/** Terminal-only geometry that never enters the shared session projection. */
export interface TuiLayoutState {
  readonly graphWidthOverride: number | null;
  readonly chatWidthOverride: number | null;
}

export function initialTuiLayoutState(): TuiLayoutState {
  return {graphWidthOverride: null, chatWidthOverride: null};
}

export function withGraphWidthOverride(
  state: TuiLayoutState,
  width: number | null,
): TuiLayoutState {
  return state.graphWidthOverride === width ? state : {...state, graphWidthOverride: width};
}

export function withChatWidthOverride(state: TuiLayoutState, width: number | null): TuiLayoutState {
  return state.chatWidthOverride === width ? state : {...state, chatWidthOverride: width};
}
