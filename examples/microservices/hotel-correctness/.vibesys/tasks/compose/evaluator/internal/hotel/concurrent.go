package hotel

import (
	"context"
	"fmt"
	"math/rand"

	"vibesys/microservice-evaluator/accuracy"
)

// verifyConcurrentIsolation describes the property as one event program. The
// generic verifier issues the parallel group concurrently and accepts only an
// observation set admitted by some ordering of the canonical Hotel model.
func (a *Application) verifyConcurrentIsolation(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
) (int, error) {
	username, password := MustUser(0)
	rateIDs := sortedRateBackedIDs()
	random.Shuffle(len(rateIDs), func(left, right int) {
		rateIDs[left], rateIDs[right] = rateIDs[right], rateIDs[left]
	})
	hotels := rateIDs[:min(6, len(rateIDs))]
	sortHotelIDs(hotels)
	if len(hotels) > accuracy.MaxParallelCalls {
		return 0, fmt.Errorf(
			"Hotel isolation width %d exceeds framework limit %d",
			len(hotels),
			accuracy.MaxParallelCalls,
		)
	}
	night := namespacedNight(seed, concurrentIsolationOffset)

	calls := make([]accuracy.Call[differentialAction], 0, len(hotels))
	for index, hotelID := range hotels {
		calls = append(calls, accuracy.Call[differentialAction]{
			ID: fmt.Sprintf("isolation-reserve-%d", index),
			Action: differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				hotelID, night, 1, fmt.Sprintf("concurrent-isolation-%d", index), username, password,
			)},
		})
	}
	steps := []accuracy.Step[differentialAction]{parallelStep(calls...)}
	for index, hotelID := range hotels {
		probes, err := capacityProbeSteps(
			hotelID, night, 1, fmt.Sprintf("isolation-probe-%d", index), username, password,
		)
		if err != nil {
			return 0, err
		}
		steps = append(steps, probes...)
	}
	return a.verifyHotelProgram(ctx, c, accuracy.Program[differentialAction]{
		SchemaVersion: accuracy.ProgramSchemaVersion,
		ID:            "hotel-concurrent-isolation",
		Steps:         steps,
	}, nil, nil)
}

// verifyLinearizableCapacity generates contended event programs. The shared
// verifier explores legal serializations of each burst against hotelOracle, so
// this check contains no separate acknowledgement-count oracle.
func (a *Application) verifyLinearizableCapacity(
	ctx context.Context,
	c client,
	seed int64,
	cases int,
	random *rand.Rand,
) (int, error) {
	username, password := MustUser(0)
	rateIDs := sortedRateBackedIDs()
	checks := 0
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		if err := checkContext(ctx); err != nil {
			return checks, err
		}
		hotelID := rateIDs[random.Intn(len(rateIDs))]
		capacity, err := capacityForHotel(hotelID)
		if err != nil {
			return checks, err
		}
		night := namespacedNight(seed, concurrentCapacityOffset+caseIndex*nightsPerCase)
		remaining := 1 + random.Intn(3)
		concurrency, err := linearizableConcurrency(remaining)
		if err != nil {
			return checks, err
		}
		fill := differentialAction{Kind: actionReserve, Query: reservationActionQuery(
			hotelID, night, capacity-remaining,
			fmt.Sprintf("linearizable-%d-fill", caseIndex), username, password,
		)}
		burst := make([]accuracy.Call[differentialAction], 0, concurrency)
		for index := 0; index < concurrency; index++ {
			burst = append(burst, accuracy.Call[differentialAction]{
				ID: fmt.Sprintf("race-%d-%d", caseIndex, index),
				Action: differentialAction{Kind: actionReserve, Query: reservationActionQuery(
					hotelID, night, 1,
					fmt.Sprintf("linearizable-%d-race-%d", caseIndex, index), username, password,
				)},
			})
		}
		steps := []accuracy.Step[differentialAction]{
			callStep(fmt.Sprintf("fill-%d", caseIndex), fill),
			parallelStep(burst...),
		}
		probes, err := capacityProbeSteps(
			hotelID, night, capacity, fmt.Sprintf("linearizable-%d-probe", caseIndex), username, password,
		)
		if err != nil {
			return checks, err
		}
		steps = append(steps, probes...)
		caseChecks, err := a.verifyHotelProgram(ctx, c, accuracy.Program[differentialAction]{
			SchemaVersion: accuracy.ProgramSchemaVersion,
			ID:            fmt.Sprintf("hotel-linearizable-capacity-%d", caseIndex),
			Steps:         steps,
		}, nil, nil)
		checks += caseChecks
		if err != nil {
			return checks, err
		}
	}
	return checks, nil
}

func linearizableConcurrency(remaining int) (int, error) {
	// Version 1 programs bound parallel groups to eight calls. Six to eight
	// calls still oversubscribe every generated remainder.
	concurrency := remaining + 5
	if concurrency > accuracy.MaxParallelCalls {
		return 0, fmt.Errorf(
			"Hotel contention width %d exceeds framework limit %d",
			concurrency,
			accuracy.MaxParallelCalls,
		)
	}
	return concurrency, nil
}

func capacityProbeSteps(
	hotelID string,
	night [2]string,
	reserved int,
	label string,
	username string,
	password string,
) ([]accuracy.Step[differentialAction], error) {
	capacity, err := capacityForHotel(hotelID)
	if err != nil {
		return nil, err
	}
	remaining := capacity - reserved
	steps := []accuracy.Step[differentialAction]{callStep(label+"-over", differentialAction{
		Kind: actionReserve,
		Query: reservationActionQuery(
			hotelID, night, remaining+1, label+"-over", username, password,
		),
	})}
	if remaining > 0 {
		steps = append(steps, callStep(label+"-exact", differentialAction{
			Kind: actionReserve,
			Query: reservationActionQuery(
				hotelID, night, remaining, label+"-exact", username, password,
			),
		}))
	}
	return steps, nil
}

func (a *Application) verifyHotelProgram(
	ctx context.Context,
	c client,
	program accuracy.Program[differentialAction],
	crash func(context.Context) error,
	start func(context.Context) error,
) (int, error) {
	oracle, err := newHotelOracle(a.catalog)
	if err != nil {
		return 0, err
	}
	trace, err := accuracy.VerifyProgram(ctx, program, oracle, a.programCandidate(c, crash, start))
	return traceCallCount(trace), err
}

// probeRemaining remains a direct helper for the specialized edge-case checks
// in edges.go. Event-program checks use capacityProbeSteps instead.
func (a *Application) probeRemaining(
	ctx context.Context,
	c client,
	hotelID string,
	night [2]string,
	reserved int,
	label string,
) (int, error) {
	username, password := MustUser(0)
	capacity, err := capacityForHotel(hotelID)
	if err != nil {
		return 0, err
	}
	remaining := capacity - reserved
	over := reservationActionQuery(hotelID, night, remaining+1, label+"-over", username, password)
	if err := c.exactMessage(ctx, "/reservation", over, reservationFailure); err != nil {
		return 1, fmt.Errorf("hotel %s night %s consumed fewer than %d rooms: %w", hotelID, night[0], reserved, err)
	}
	if remaining == 0 {
		return 1, nil
	}
	exact := reservationActionQuery(hotelID, night, remaining, label+"-exact", username, password)
	if err := c.exactMessage(ctx, "/reservation", exact, reservationSuccess); err != nil {
		return 2, fmt.Errorf("hotel %s night %s consumed more than %d rooms: %w", hotelID, night[0], reserved, err)
	}
	return 2, nil
}
