package hotel

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math/rand"
	"sort"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

const (
	loginSuccess       = "Login successfully!"
	loginFailure       = "Failed. Please check your username and password. "
	reservationSuccess = "Reserve successfully!"
	reservationFailure = "Failed. Already reserved. "
)

func (a *Application) Check(
	ctx context.Context,
	runtime api.Runtime,
	check api.AccuracyContext,
	recorder api.AccuracyRecorder,
) error {
	c := client{runtime: runtime, timeout: a.timeout}
	random := rand.New(rand.NewSource(check.Seed ^ 0x51f15e))

	checks, err := a.verifySearch(ctx, c, random, max(check.Cases, 4))
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("strict_geojson_schema", "search_semantics"); err != nil {
		return err
	}

	checks, err = a.verifyRecommendations(ctx, c, random)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("seeded_profile_semantics", "recommendation_semantics"); err != nil {
		return err
	}

	checks, err = a.verifyAuthentication(ctx, c, random, max(check.Cases, 4))
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("authentication_semantics"); err != nil {
		return err
	}

	checks, err = a.verifyNegativeCases(ctx, c, random)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}

	checks, err = a.verifySearchAvailability(ctx, c, check.Seed, random)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("search_availability"); err != nil {
		return err
	}

	checks, err = a.verifyOptionalReservationNumber(ctx, c, check.Seed, random)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("reservation_optional_number"); err != nil {
		return err
	}

	checks, err = a.verifyReservations(ctx, c, check.Seed, check.Cases, random)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := recorder.Pass("reservation_capacity", "read_your_write", "reservation_isolation"); err != nil {
		return err
	}
	if check.Restart == nil {
		return nil
	}
	checks, err = a.verifyCrashRecovery(ctx, c, check.Seed, random, check.Restart)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	return recorder.Pass("crash_recovery")
}

func (a *Application) verifySearch(ctx context.Context, c client, random *rand.Rand, cases int) (int, error) {
	checks := 0
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		if err := checkContext(ctx); err != nil {
			return checks, err
		}
		inDay := 9 + random.Intn(15)
		outDay := inDay + 1 + random.Intn(24-inDay)
		anchor := a.catalog[strconv.Itoa(7+random.Intn(74))]
		jitter := func() float64 { return (random.Float64()*2 - 1) / 1000.0 }
		query := map[string]string{
			"inDate":  fmt.Sprintf("2015-04-%02d", inDay),
			"outDate": fmt.Sprintf("2015-04-%02d", outDay),
			"lat":     strconv.FormatFloat(anchor.recommendLat+jitter(), 'f', 6, 64),
			"lon":     strconv.FormatFloat(anchor.recommendLon+jitter(), 'f', 6, 64),
		}
		if caseIndex%2 == 0 {
			query["locale"] = "en"
		}
		first, err := c.geoJSON(ctx, "/hotels", query)
		checks++
		if err != nil {
			return checks, err
		}
		if err := a.validateSearchSet(first); err != nil {
			return checks, fmt.Errorf("fuzzed search case %d query %v: %w", caseIndex, query, err)
		}
		second, err := c.geoJSON(ctx, "/hotels", query)
		checks++
		if err != nil {
			return checks, err
		}
		if err := a.validateSearchSet(second); err != nil {
			return checks, fmt.Errorf("warm fuzzed search case %d query %v: %w", caseIndex, query, err)
		}
		if err := sameIDs(first, second); err != nil {
			return checks, fmt.Errorf("fuzzed search case %d cache stability: %w", caseIndex, err)
		}
	}
	return checks, nil
}

func (a *Application) validateSearchSet(features map[string]feature) error {
	if err := validateProfiles(features, a.catalog, "canonical search"); err != nil {
		return err
	}
	expected := rateBackedIDs()
	ids := make([]string, 0, len(expected))
	for id := range expected {
		ids = append(ids, id)
	}
	if err := exactIDs(features, ids...); err != nil {
		return fmt.Errorf("rate-backed result set: %w", err)
	}
	return nil
}

func (a *Application) verifyRecommendations(ctx context.Context, c client, random *rand.Rand) (int, error) {
	checks := 0
	queries := []struct {
		require string
		ids     []string
	}{
		{require: "price", ids: []string{"2"}},
		{require: "rate", ids: []string{"9", "24", "39", "54", "69"}},
	}
	for queryIndex, item := range queries {
		query := map[string]string{"require": item.require, "lat": "37.7749", "lon": "-122.4194"}
		if queryIndex%2 == 0 {
			query["locale"] = "en"
		}
		features, err := c.geoJSON(ctx, "/recommendations", query)
		checks++
		if err != nil {
			return checks, err
		}
		if err := validateProfiles(features, a.catalog, item.require+" recommendation"); err != nil {
			return checks, err
		}
		if err := exactIDs(features, item.ids...); err != nil {
			return checks, fmt.Errorf("%s recommendation: %w", item.require, err)
		}
	}

	// Query every seeded coordinate in randomized order. This makes the 80-row
	// catalog observable and proves that distance recommendation returns the
	// unique exact-location hotel, rather than merely accepting a known subset.
	ids := make([]int, 80)
	for index := range ids {
		ids[index] = index + 1
	}
	random.Shuffle(len(ids), func(left, right int) { ids[left], ids[right] = ids[right], ids[left] })
	for _, numericID := range ids {
		if err := checkContext(ctx); err != nil {
			return checks, err
		}
		id := strconv.Itoa(numericID)
		expected := a.catalog[id]
		query := map[string]string{
			"require": "dis",
			"lat":     strconv.FormatFloat(expected.recommendLat, 'f', -1, 64),
			"lon":     strconv.FormatFloat(expected.recommendLon, 'f', -1, 64),
		}
		if numericID%2 == 0 {
			query["locale"] = "en"
		}
		features, err := c.geoJSON(ctx, "/recommendations", query)
		checks++
		if err != nil {
			return checks, err
		}
		if err := validateProfiles(features, a.catalog, "distance recommendation"); err != nil {
			return checks, err
		}
		if err := exactIDs(features, id); err != nil {
			return checks, fmt.Errorf("distance recommendation at hotel %s coordinates: %w", id, err)
		}
	}

	// Probe non-catalog coordinates that are derived from the runtime seed.
	// Each point stays within a tiny neighborhood of a generated hotel, whose
	// nearest-neighbor margin is much larger than the jitter. This frustrates
	// lookup tables keyed only by the 80 public seed coordinates while keeping
	// the oracle independent and unambiguous.
	for caseIndex := 0; caseIndex < 2*max(4, len(ids)/20); caseIndex++ {
		numericID := 7 + random.Intn(74)
		id := strconv.Itoa(numericID)
		expected := a.catalog[id]
		jitter := func() float64 { return (random.Float64()*2 - 1) / 100000.0 }
		query := map[string]string{
			"require": "dis",
			"lat":     strconv.FormatFloat(expected.recommendLat+jitter(), 'f', 8, 64),
			"lon":     strconv.FormatFloat(expected.recommendLon+jitter(), 'f', 8, 64),
		}
		if caseIndex%2 == 0 {
			query["locale"] = "en"
		}
		features, err := c.geoJSON(ctx, "/recommendations", query)
		checks++
		if err != nil {
			return checks, err
		}
		if err := validateProfiles(features, a.catalog, "fuzzed distance recommendation"); err != nil {
			return checks, err
		}
		if err := exactIDs(features, id); err != nil {
			return checks, fmt.Errorf("fuzzed distance recommendation near hotel %s: %w", id, err)
		}
	}
	return checks, nil
}

func (a *Application) verifyAuthentication(ctx context.Context, c client, random *rand.Rand, cases int) (int, error) {
	checks := 0
	indices := random.Perm(501)
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		index := indices[caseIndex%len(indices)]
		if err := checkContext(ctx); err != nil {
			return checks, err
		}
		username, password, err := hotelsupport.User(index)
		if err != nil {
			return checks, err
		}
		if err := c.exactMessage(ctx, "/user", credentials(username, password), loginSuccess); err != nil {
			return checks + 1, fmt.Errorf("valid seed user %d: %w", index, err)
		}
		checks++
		if err := c.exactMessage(ctx, "/user", credentials(username, password+"-wrong"), loginFailure); err != nil {
			return checks + 1, fmt.Errorf("wrong password for seed user %d: %w", index, err)
		}
		checks++
	}
	if err := c.exactMessage(ctx, "/user", credentials("Cornell_nonexistent", "not-a-password"), loginFailure); err != nil {
		return checks + 1, fmt.Errorf("nonexistent user: %w", err)
	}
	return checks + 1, nil
}

func (a *Application) verifyNegativeCases(ctx context.Context, c client, random *rand.Rand) (int, error) {
	username, password := hotelsupport.MustUser(0)
	checks := 0
	reservationBase := map[string]string{
		"inDate": "3000-01-01", "outDate": "3000-01-02", "hotelId": "1",
		"customerName": "negative-case", "username": username, "password": password, "number": "1",
	}
	negative := []struct {
		path  string
		query map[string]string
	}{
		{"/hotels", map[string]string{"lat": "37", "lon": "-122"}},
		{"/hotels", map[string]string{"inDate": "2015-04-09", "outDate": "2015-04-10"}},
		{"/recommendations", map[string]string{"require": "price"}},
		{"/recommendations", map[string]string{
			"lat": "37", "lon": "-122", "require": fmt.Sprintf("unknown-%016x", random.Uint64()),
		}},
		{"/user", map[string]string{"username": username}},
		{"/reservation", without(reservationBase, "inDate")},
		{"/reservation", with(reservationBase, "inDate", fmt.Sprintf("invalid-%016x", random.Uint64()))},
		{"/reservation", without(reservationBase, "hotelId")},
		{"/reservation", without(reservationBase, "customerName")},
		{"/reservation", without(reservationBase, "password")},
	}
	random.Shuffle(len(negative), func(left, right int) { negative[left], negative[right] = negative[right], negative[left] })
	for _, item := range negative {
		if err := c.badRequest(ctx, item.path, item.query); err != nil {
			return checks + 1, err
		}
		checks++
	}
	return checks, nil
}

func (a *Application) verifyOptionalReservationNumber(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
) (int, error) {
	username, password := hotelsupport.MustUser(0)
	hotelID := 1 + random.Intn(80)
	capacity, err := hotelsupport.Capacity(hotelID)
	if err != nil {
		return 0, err
	}
	start := reservationDate(seed).AddDate(0, 0, 180)
	query := credentials(username, password)
	query["hotelId"] = strconv.Itoa(hotelID)
	query["inDate"] = start.Format(time.DateOnly)
	query["outDate"] = start.AddDate(0, 0, 1).Format(time.DateOnly)
	query["customerName"] = "optional-number"
	if err := c.exactMessage(ctx, "/reservation", query, reservationSuccess); err != nil {
		return 1, fmt.Errorf("reservation without optional number: %w", err)
	}
	query["number"] = strconv.Itoa(capacity)
	query["customerName"] = "optional-number-capacity"
	if err := c.exactMessage(ctx, "/reservation", query, reservationSuccess); err != nil {
		return 2, fmt.Errorf("omitted number unexpectedly consumed capacity: %w", err)
	}
	query["number"] = "1"
	query["customerName"] = "optional-number-over-capacity"
	if err := c.exactMessage(ctx, "/reservation", query, reservationFailure); err != nil {
		return 3, fmt.Errorf("optional-number capacity read-back: %w", err)
	}
	return 3, nil
}

func (a *Application) verifySearchAvailability(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
) (int, error) {
	username, password := hotelsupport.MustUser(0)
	rateIDs := sortedRateBackedIDs()
	hotelID := rateIDs[random.Intn(len(rateIDs))]
	numericID, _ := strconv.Atoi(hotelID)
	capacity, err := hotelsupport.Capacity(numericID)
	if err != nil {
		return 0, err
	}
	start := reservationDate(seed).AddDate(0, 0, 365)
	reservedDate := [2]string{start.Format(time.DateOnly), start.AddDate(0, 0, 1).Format(time.DateOnly)}
	query := credentials(username, password)
	query["hotelId"] = hotelID
	query["inDate"] = reservedDate[0]
	query["outDate"] = reservedDate[1]
	query["number"] = strconv.Itoa(capacity)
	query["customerName"] = "search-availability"
	if err := c.exactMessage(ctx, "/reservation", query, reservationSuccess); err != nil {
		return 1, fmt.Errorf("search availability exact-capacity setup: %w", err)
	}
	overCapacity := cloneQuery(query)
	overCapacity["number"] = "1"
	if err := c.exactMessage(ctx, "/reservation", overCapacity, reservationFailure); err != nil {
		return 2, fmt.Errorf("search availability setup visibility: %w", err)
	}

	anchor := a.catalog[hotelID]
	search := func(date [2]string) (map[string]feature, error) {
		return c.geoJSON(ctx, "/hotels", map[string]string{
			"inDate": date[0], "outDate": date[1],
			"lat":    strconv.FormatFloat(anchor.recommendLat, 'f', -1, 64),
			"lon":    strconv.FormatFloat(anchor.recommendLon, 'f', -1, 64),
			"locale": "en",
		})
	}
	reserved, err := search(reservedDate)
	if err != nil {
		return 3, err
	}
	if err := validateProfiles(reserved, a.catalog, "reserved-date search"); err != nil {
		return 3, err
	}
	wanted := make([]string, 0, len(rateIDs)-1)
	for _, id := range rateIDs {
		if id != hotelID {
			wanted = append(wanted, id)
		}
	}
	if err := exactIDs(reserved, wanted...); err != nil {
		return 3, fmt.Errorf("reserved-date search for filled hotel %s: %w", hotelID, err)
	}
	adjacentDate := [2]string{
		start.AddDate(0, 0, 1).Format(time.DateOnly),
		start.AddDate(0, 0, 2).Format(time.DateOnly),
	}
	adjacent, err := search(adjacentDate)
	if err != nil {
		return 4, err
	}
	if err := validateProfiles(adjacent, a.catalog, "adjacent-date search"); err != nil {
		return 4, err
	}
	if err := exactIDs(adjacent, rateIDs...); err != nil {
		return 4, fmt.Errorf("adjacent-date search after filling hotel %s: %w", hotelID, err)
	}
	return 4, nil
}

func sortedRateBackedIDs() []string {
	ids := make([]string, 0, len(rateBackedIDs()))
	for id := range rateBackedIDs() {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(left, right int) bool {
		leftID, _ := strconv.Atoi(ids[left])
		rightID, _ := strconv.Atoi(ids[right])
		return leftID < rightID
	})
	return ids
}

func (a *Application) verifyReservations(
	ctx context.Context,
	c client,
	seed int64,
	cases int,
	random *rand.Rand,
) (int, error) {
	username, password := hotelsupport.MustUser(0)
	start := reservationDate(seed)
	checks := 0
	reserve := func(hotelID string, date [2]string, rooms int, customer, expected string) error {
		query := credentials(username, password)
		query["hotelId"] = hotelID
		query["inDate"] = date[0]
		query["outDate"] = date[1]
		query["number"] = strconv.Itoa(rooms)
		query["customerName"] = customer
		checks++
		return c.exactMessage(ctx, "/reservation", query, expected)
	}

	// The endpoint has no delete operation. Use shuffled hotel pairs and
	// hash-namespaced, disjoint future date slots so each case is independent
	// without claiming cleanup that the application cannot perform.
	hotelOrder := random.Perm(80)
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		if err := checkContext(ctx); err != nil {
			return checks, err
		}
		primaryID := 1 + hotelOrder[(caseIndex*2)%len(hotelOrder)]
		isolationID := 1 + hotelOrder[(caseIndex*2+1)%len(hotelOrder)]
		caseStart := start.AddDate(0, 0, caseIndex*4)
		exactDate := [2]string{
			caseStart.Format("2006-01-02"),
			caseStart.AddDate(0, 0, 1).Format("2006-01-02"),
		}
		adjacentDate := [2]string{
			caseStart.AddDate(0, 0, 2).Format("2006-01-02"),
			caseStart.AddDate(0, 0, 3).Format("2006-01-02"),
		}
		primary := strconv.Itoa(primaryID)
		isolation := strconv.Itoa(isolationID)
		capacity, err := hotelsupport.Capacity(primaryID)
		if err != nil {
			return checks, err
		}
		label := fmt.Sprintf("case-%d", caseIndex)
		if err := reserve(primary, exactDate, capacity, label+"-exact-capacity", reservationSuccess); err != nil {
			return checks, fmt.Errorf("reservation case %d exact capacity: %w", caseIndex, err)
		}
		if err := reserve(primary, exactDate, 1, label+"-read-your-write", reservationFailure); err != nil {
			return checks, fmt.Errorf("reservation case %d immediately visible: %w", caseIndex, err)
		}
		if err := reserve(primary, adjacentDate, capacity+1, label+"-atomic-rejection", reservationFailure); err != nil {
			return checks, fmt.Errorf("reservation case %d over capacity: %w", caseIndex, err)
		}
		if err := reserve(primary, adjacentDate, 1, label+"-post-rejection", reservationSuccess); err != nil {
			return checks, fmt.Errorf("reservation case %d rejected request mutated capacity: %w", caseIndex, err)
		}
		if err := reserve(isolation, adjacentDate, 1, label+"-hotel-isolation", reservationSuccess); err != nil {
			return checks, fmt.Errorf("reservation case %d same-date hotel isolation: %w", caseIndex, err)
		}
	}
	return checks, nil
}

func (a *Application) verifyCrashRecovery(
	ctx context.Context,
	c client,
	seed int64,
	random *rand.Rand,
	restart func(context.Context) error,
) (int, error) {
	username, password := hotelsupport.MustUser(0)
	rateIDs := sortedRateBackedIDs()
	hotelID := rateIDs[random.Intn(len(rateIDs))]
	numericID, _ := strconv.Atoi(hotelID)
	capacity, err := hotelsupport.Capacity(numericID)
	if err != nil {
		return 0, err
	}
	start := reservationDate(seed).AddDate(0, 0, 730)
	query := credentials(username, password)
	query["hotelId"] = hotelID
	query["inDate"] = start.Format(time.DateOnly)
	query["outDate"] = start.AddDate(0, 0, 1).Format(time.DateOnly)
	query["number"] = strconv.Itoa(capacity)
	query["customerName"] = "crash-recovery"
	if err := c.exactMessage(ctx, "/reservation", query, reservationSuccess); err != nil {
		return 1, fmt.Errorf("crash recovery setup: %w", err)
	}
	if err := restart(ctx); err != nil {
		return 1, fmt.Errorf("restart candidate for crash recovery: %w", err)
	}
	query["number"] = "1"
	query["customerName"] = "crash-recovery-probe"
	if err := c.exactMessage(ctx, "/reservation", query, reservationFailure); err != nil {
		return 2, fmt.Errorf("acknowledged reservation lost after restart: %w", err)
	}
	anchor := a.catalog[hotelID]
	features, err := c.geoJSON(ctx, "/hotels", map[string]string{
		"inDate":  start.Format(time.DateOnly),
		"outDate": start.AddDate(0, 0, 1).Format(time.DateOnly),
		"lat":     strconv.FormatFloat(anchor.recommendLat, 'f', -1, 64),
		"lon":     strconv.FormatFloat(anchor.recommendLon, 'f', -1, 64),
		"locale":  "en",
	})
	if err != nil {
		return 3, err
	}
	if _, present := features[hotelID]; present {
		return 3, fmt.Errorf("filled hotel %s reappeared in search after restart", hotelID)
	}
	if err := validateProfiles(features, a.catalog, "post-restart search"); err != nil {
		return 3, err
	}
	return 3, nil
}

func reservationDate(seed int64) time.Time {
	digest := sha256.Sum256([]byte(fmt.Sprintf("hotel-reservation-accuracy/%d", seed)))
	year := 3000 + int(binary.BigEndian.Uint16(digest[:2]))%5000
	day := 1 + int(binary.BigEndian.Uint16(digest[2:4]))%300
	return time.Date(year, time.January, day, 0, 0, 0, 0, time.UTC)
}

func credentials(username, password string) map[string]string {
	return map[string]string{"username": username, "password": password}
}

func without(source map[string]string, key string) map[string]string {
	result := cloneQuery(source)
	delete(result, key)
	return result
}

func with(source map[string]string, key, value string) map[string]string {
	result := cloneQuery(source)
	result[key] = value
	return result
}

func cloneQuery(source map[string]string) map[string]string {
	result := make(map[string]string, len(source))
	for key, value := range source {
		result[key] = value
	}
	return result
}
