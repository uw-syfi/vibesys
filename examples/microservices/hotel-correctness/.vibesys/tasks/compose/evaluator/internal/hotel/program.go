package hotel

import (
	"context"

	"vibesys/microservice-evaluator/accuracy"
)

func callStep(id string, action differentialAction) accuracy.Step[differentialAction] {
	return accuracy.Step[differentialAction]{
		Call: &accuracy.Call[differentialAction]{ID: id, Action: action},
	}
}

func parallelStep(calls ...accuracy.Call[differentialAction]) accuracy.Step[differentialAction] {
	return accuracy.Step[differentialAction]{
		Parallel: &accuracy.Parallel[differentialAction]{Calls: calls},
	}
}

func crashStep() accuracy.Step[differentialAction] {
	return accuracy.Step[differentialAction]{Crash: &accuracy.Crash{}}
}

func startStep() accuracy.Step[differentialAction] {
	return accuracy.Step[differentialAction]{Start: &accuracy.Start{}}
}

func (a *Application) programCandidate(
	c client,
	crash func(context.Context) error,
	start func(context.Context) error,
) accuracy.Candidate[differentialAction, differentialObservation] {
	return accuracy.Candidate[differentialAction, differentialObservation]{
		Invoke: func(ctx context.Context, action differentialAction) (differentialObservation, error) {
			return a.observeDifferentialAction(ctx, c, action)
		},
		Crash: crash,
		Start: start,
	}
}

func traceCallCount(trace accuracy.Trace[differentialObservation]) int {
	count := 0
	for _, step := range trace.Steps {
		count += len(step.Calls)
	}
	return count
}
