package flow

import "context"

// Shell carries out the machine's requests: it is every place the flow touches the machine it
// runs on. Do always answers; a request that failed is answered with the event that says so.
type Shell interface {
	Do(ctx context.Context, r Request) Event
}

// Drive steps the machine from the plan to its verdict, asking the shell for each request. A
// context that is done before a request is carried out answers it with Cancelled, so a cancel
// between two commands ends the job as a cancel and not as whatever the next command would have
// done. It fails only for a plan that is not valid.
//
// It takes at most four steps and two for each sample, whatever the shell answers: an answer that
// fits the request advances the machine and one that does not ends it.
func Drive(ctx context.Context, p Plan, sh Shell) (Verdict, error) {
	state, req, err := Start(p)
	if err != nil {
		return Verdict{}, err
	}
	for req != nil {
		var e Event = Cancelled{}
		if ctx.Err() == nil {
			e = sh.Do(ctx, req)
		}
		state, req = Step(state, e)
	}
	return state.Verdict(), nil
}
