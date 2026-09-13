package hotel

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
)

// verifyDegenerateDateRanges pins the reservation semantics of ranges that
// enclose no nights. The pinned frontend validates only the YYYY-MM-DD shape,
// so an empty or reversed range walks zero nights: it is acknowledged and
// consumes nothing. A candidate that silently rejects, or that charges a night
// anyway, changes observable behavior even though no obvious endpoint moved.
func (a *Application) verifyDegenerateDateRanges(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
) (int, error) {
	username, password := MustUser(0)
	rateIDs := sortedRateBackedIDs()
	hotelID := rateIDs[random.Intn(len(rateIDs))]
	capacity, err := capacityForHotel(hotelID)
	if err != nil {
		return 0, err
	}
	night := namespacedNight(seed, degenerateOffset)
	checks := 0
	ranges := []struct {
		name  string
		dates [2]string
	}{
		{"empty range", [2]string{night[0], night[0]}},
		{"reversed range", [2]string{night[1], night[0]}},
	}
	for _, item := range ranges {
		query := reservationActionQuery(
			hotelID, item.dates, capacity+1, "degenerate-"+item.name, username, password,
		)
		checks++
		if err := c.exactMessage(ctx, "/reservation", query, reservationSuccess); err != nil {
			return checks, fmt.Errorf("%s reservation for hotel %s: %w", item.name, hotelID, err)
		}
	}
	// A range that encloses no nights must not have consumed capacity on the
	// dates it names, in either order.
	probes, err := a.probeRemaining(ctx, c, hotelID, night, 0, "degenerate-probe")
	checks += probes
	if err != nil {
		return checks, fmt.Errorf("degenerate date range consumed capacity on hotel %s: %w", hotelID, err)
	}
	return checks, nil
}

// verifyMultiNightAtomicity books a range whose interior night is already full.
// The whole range must be rejected, and the nights that had room must remain
// untouched: a partial application would leave capacity consumed for a stay the
// caller was told it did not get.
func (a *Application) verifyMultiNightAtomicity(
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
		start := reservationDate(seed).AddDate(0, 0, multiNightOffset+caseIndex*nightsPerCase)
		day := func(offset int) string { return start.AddDate(0, 0, offset).Format(time.DateOnly) }
		label := fmt.Sprintf("multi-night-%d", caseIndex)

		interior := [2]string{day(1), day(2)}
		checks++
		if err := c.exactMessage(ctx, "/reservation", reservationActionQuery(
			hotelID, interior, capacity, label+"-fill-interior", username, password,
		), reservationSuccess); err != nil {
			return checks, fmt.Errorf("multi-night case %d interior setup: %w", caseIndex, err)
		}

		span := [2]string{day(0), day(3)}
		checks++
		if err := c.exactMessage(ctx, "/reservation", reservationActionQuery(
			hotelID, span, 1, label+"-span", username, password,
		), reservationFailure); err != nil {
			return checks, fmt.Errorf(
				"multi-night case %d span over a full interior night was accepted: %w", caseIndex, err,
			)
		}

		// Neither flanking night may have absorbed part of the rejected stay.
		for offset, flank := range [][2]string{{day(0), day(1)}, {day(2), day(3)}} {
			probes, err := a.probeRemaining(
				ctx, c, hotelID, flank, 0, fmt.Sprintf("%s-flank-%d", label, offset),
			)
			checks += probes
			if err != nil {
				return checks, fmt.Errorf(
					"multi-night case %d rejected span consumed flanking night %s: %w", caseIndex, flank[0], err,
				)
			}
		}
	}
	return checks, nil
}

// verifyEndpointLiveness sends fixture arguments that no seeded row can satisfy
// and requires the gateway to keep serving. The pinned upstream reservation
// service calls log.Panic when the capacity lookup finds no document, so one
// request for an out-of-catalog hotel takes the endpoint down until the
// container restarts. Any candidate that keeps that shape turns a malformed
// request into an outage, so this property is opt-in rather than a silent
// baseline regression.
func (a *Application) verifyEndpointLiveness(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
) (int, error) {
	username, password := MustUser(0)
	night := namespacedNight(seed, livenessOffset)
	unknown := strconv.Itoa(1_000_000 + random.Intn(1_000_000))
	query := reservationActionQuery(unknown, night, 1, "liveness-unknown-hotel", username, password)

	result := c.request(ctx, "/reservation", query)
	checks := 1
	if response, err := httpcheck.Response(result, http.StatusOK); err == nil {
		if message, ok := decodeMessage(response.Body); ok && message == reservationSuccess {
			return checks, fmt.Errorf(
				"reservation for out-of-catalog hotel %s was acknowledged", unknown,
			)
		}
	}

	// Whatever the endpoint answered, it must still be serving. A readiness
	// probe would mask this, so the check re-issues live application traffic.
	checks++
	if err := c.exactMessage(ctx, "/user", credentials(username, password), loginSuccess); err != nil {
		return checks, fmt.Errorf(
			"gateway stopped serving after a request for out-of-catalog hotel %s: %w", unknown, err,
		)
	}
	checks++
	known := reservationActionQuery("1", night, 1, "liveness-known-hotel", username, password)
	if err := c.exactMessage(ctx, "/reservation", known, reservationSuccess); err != nil {
		return checks, fmt.Errorf(
			"reservation endpoint stopped serving after a request for out-of-catalog hotel %s: %w", unknown, err,
		)
	}
	return checks, nil
}

func decodeMessage(body []byte) (string, bool) {
	decoded, err := httpcheck.DecodeJSON(body)
	if err != nil {
		return "", false
	}
	object, ok := decoded.(map[string]any)
	if !ok {
		return "", false
	}
	message, ok := object["message"].(string)
	return message, ok
}
