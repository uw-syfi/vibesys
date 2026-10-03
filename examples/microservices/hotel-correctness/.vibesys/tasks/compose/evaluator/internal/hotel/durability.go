package hotel

import (
	"context"
	"fmt"
	"math/rand"

	"vibesys/microservice-evaluator/accuracy"
)

// durableNight identifies application fixtures used by a durability program.
type durableNight struct {
	FullHotel    string
	PartialHotel string
	Night        [2]string
}

// verifyDurableState generates one program containing acknowledged writes,
// crash/start events, and post-restart observations. hotelOracle.AfterCrash is
// the single durability specification: it preserves acknowledged reservation
// state, and subsequent ordinary Hotel actions expose whether the candidate did
// the same.
func (a *Application) verifyDurableState(
	ctx context.Context,
	c client,
	seed int64,
	cases int,
	random *rand.Rand,
	crash func(context.Context) error,
	start func(context.Context) error,
	checkAvailability bool,
) (int, error) {
	username, password := MustUser(0)
	rateIDs := sortedRateBackedIDs()
	random.Shuffle(len(rateIDs), func(left, right int) {
		rateIDs[left], rateIDs[right] = rateIDs[right], rateIDs[left]
	})
	if len(rateIDs) < 2 {
		return 0, fmt.Errorf("Hotel durability check needs at least two rate-backed hotels")
	}

	nights := make([]durableNight, 0, cases)
	steps := make([]accuracy.Step[differentialAction], 0, cases*8+2)
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		if err := checkContext(ctx); err != nil {
			return 0, err
		}
		night := durableNight{
			FullHotel:    rateIDs[(caseIndex*2)%len(rateIDs)],
			PartialHotel: rateIDs[(caseIndex*2+1)%len(rateIDs)],
			Night:        namespacedNight(seed, durabilityOffset+caseIndex*nightsPerCase),
		}
		if night.FullHotel == night.PartialHotel {
			continue
		}
		for _, item := range []struct {
			hotelID string
			slack   int
			label   string
		}{
			{night.FullHotel, 0, "full"},
			{night.PartialHotel, 1, "partial"},
		} {
			capacity, err := capacityForHotel(item.hotelID)
			if err != nil {
				return 0, err
			}
			steps = append(steps, callStep(
				fmt.Sprintf("durable-%d-%s-write", caseIndex, item.label),
				differentialAction{Kind: actionReserve, Query: reservationActionQuery(
					item.hotelID, night.Night, capacity-item.slack,
					fmt.Sprintf("durable-%d-%s", caseIndex, item.label), username, password,
				)},
			))
		}
		nights = append(nights, night)
	}
	if len(nights) == 0 {
		return 0, fmt.Errorf("Hotel durability check generated no nights")
	}

	steps = append(steps, crashStep(), startStep())
	for caseIndex, night := range nights {
		if checkAvailability {
			steps = append(steps, callStep(
				fmt.Sprintf("durable-%d-search", caseIndex),
				differentialAction{
					Kind:  actionSearch,
					Query: searchQuery(night.Night, a.catalog[night.FullHotel], true),
				},
			))
		}
		fullCapacity, err := capacityForHotel(night.FullHotel)
		if err != nil {
			return 0, err
		}
		partialCapacity, err := capacityForHotel(night.PartialHotel)
		if err != nil {
			return 0, err
		}
		for _, item := range []struct {
			hotelID  string
			reserved int
			label    string
		}{
			{night.FullHotel, fullCapacity, "full"},
			{night.PartialHotel, partialCapacity - 1, "partial"},
		} {
			probes, err := capacityProbeSteps(
				item.hotelID,
				night.Night,
				item.reserved,
				fmt.Sprintf("durable-%d-%s-probe", caseIndex, item.label),
				username,
				password,
			)
			if err != nil {
				return 0, err
			}
			steps = append(steps, probes...)
		}
	}
	return a.verifyHotelProgram(ctx, c, accuracy.Program[differentialAction]{
		SchemaVersion: accuracy.ProgramSchemaVersion,
		ID:            "hotel-durable-state",
		Steps:         steps,
	}, crash, start)
}
