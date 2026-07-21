package hotel

import (
	"fmt"
	"math"
	"net/http"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

const (
	searchArrivalFirstDay  = 9
	searchArrivalLastDay   = 23
	searchDepartureLastDay = 24
	latitudeChoices        = 482
	longitudeChoices       = 326
)

var expectedRecommendationIDs = map[string][]string{
	operationRecommendRate:  {"9", "24", "39", "54", "69"},
	operationRecommendPrice: {"2"},
}

func (a *Application) BuildOperation(
	operation api.Operation,
	sample api.Sample,
	prepared any,
) (api.OperationPlan, error) {
	data, ok := prepared.(*fixture)
	if !ok || data == nil {
		return api.OperationPlan{}, fmt.Errorf("Hotel fixture is not prepared")
	}
	switch operation.Name {
	case operationSearch:
		return oneRequestPlan(operation, searchRequest(sample.Random), rateBackedHotelIDs()), nil
	case operationRecommendDistance, operationRecommendRate, operationRecommendPrice:
		request, lat, lon := recommendationRequest(operation.Name, sample.Random)
		expectedIDs := expectedRecommendationIDs[operation.Name]
		if operation.Name == operationRecommendDistance {
			expectedIDs = nearestRecommendationIDs(lat, lon)
		}
		return oneRequestPlan(
			operation,
			request,
			expectedIDs,
		), nil
	case operationLoginValid, operationLoginInvalid:
		return a.loginPlan(operation, sample)
	case operationReserveCapacity:
		return a.reservationPlan(operation, sample, data)
	default:
		return api.OperationPlan{}, fmt.Errorf("unknown Hotel operation %q", operation.Name)
	}
}

func oneRequestPlan(
	operation api.Operation,
	request api.HTTPRequestSpec,
	expectedIDs []string,
) api.OperationPlan {
	return api.OperationPlan{
		Invocations: []api.Invocation{{
			Target: operation.Target, Operation: operation.Name, Payload: request,
		}},
		State: &operationState{
			kind: operation.Name, expectedIDs: append([]string(nil), expectedIDs...),
		},
	}
}

func searchRequest(random uint64) api.HTTPRequestSpec {
	inDay := searchArrivalFirstDay + fuzzChoice(random, 0, searchArrivalLastDay-searchArrivalFirstDay+1)
	outDay := inDay + 1 + fuzzChoice(random, 1, searchDepartureLastDay-inDay)
	lat, lon := canonicalSearchLocation(random)
	query := map[string]string{
		"inDate":  fmt.Sprintf("2015-04-%02d", inDay),
		"outDate": fmt.Sprintf("2015-04-%02d", outDay),
		"lat":     formatCoordinate(lat), "lon": formatCoordinate(lon),
	}
	if fuzzChoice(random, 5, 2) == 1 {
		query["locale"] = "en"
	}
	return api.HTTPRequestSpec{
		Method: http.MethodGet,
		Path:   "/hotels",
		Query:  query,
	}
}

func recommendationRequest(operation string, random uint64) (api.HTTPRequestSpec, float64, float64) {
	require := map[string]string{
		operationRecommendDistance: "dis",
		operationRecommendRate:     "rate",
		operationRecommendPrice:    "price",
	}[operation]
	lat, lon := canonicalRecommendationLocation(random)
	latText, lonText := formatCoordinate(lat), formatCoordinate(lon)
	// Calculate the oracle from the values the frontend will parse, not from
	// pre-format floating-point intermediates that are not sent on the wire.
	lat = mustParseCoordinate(latText)
	lon = mustParseCoordinate(lonText)
	query := map[string]string{
		"lat": latText, "lon": lonText, "require": require,
	}
	if fuzzChoice(random, 4, 2) == 1 {
		query["locale"] = "en"
	}
	return api.HTTPRequestSpec{
		Method: http.MethodGet,
		Path:   "/recommendations",
		Query:  query,
	}, lat, lon
}

func canonicalSearchLocation(random uint64) (float64, float64) {
	// Anchoring the search around a seeded hotel guarantees a geo hit while the
	// jitter prevents candidates from recognizing a small list of exact inputs.
	// IDs 20 through 60 keep the entire jitter window inside the official wrk2
	// coordinate rectangle.
	anchorID := 20 + fuzzChoice(random, 2, 41)
	anchor, err := profileForID(strconv.Itoa(anchorID))
	if err != nil {
		panic(err)
	}
	latJitter := float64(fuzzChoice(random, 3, 21)-10) / 10000
	lonJitter := float64(fuzzChoice(random, 4, 21)-10) / 10000
	return anchor.recommendLat + latJitter, anchor.recommendLon + lonJitter
}

func canonicalRecommendationLocation(random uint64) (float64, float64) {
	latitudeStep := fuzzChoice(random, 2, latitudeChoices)
	longitudeStep := fuzzChoice(random, 3, longitudeChoices)
	lat := 38.0235 + (float64(latitudeStep)-240.5)/1000.0
	lon := -122.095 + (float64(longitudeStep)-157.0)/1000.0
	return lat, lon
}

func fuzzChoice(random, stream uint64, choices int) int {
	value := random + 0x9e3779b97f4a7c15*(stream+1)
	value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9
	value = (value ^ (value >> 27)) * 0x94d049bb133111eb
	value ^= value >> 31
	return int(value % uint64(choices))
}

func formatCoordinate(value float64) string {
	return strconv.FormatFloat(value, 'f', 4, 64)
}

func mustParseCoordinate(value string) float64 {
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		panic(err)
	}
	return parsed
}

func nearestRecommendationIDs(lat, lon float64) []string {
	minimum := math.MaxFloat64
	ids := make([]string, 0, 1)
	for number := 1; number <= 80; number++ {
		item, err := profileForID(strconv.Itoa(number))
		if err != nil {
			panic(err)
		}
		distance := recommendationDistance(lat, lon, item.recommendLat, item.recommendLon)
		switch {
		case distance < minimum:
			minimum = distance
			ids = append(ids[:0], item.id)
		case distance == minimum:
			ids = append(ids, item.id)
		}
	}
	return ids
}

func recommendationDistance(lat1, lon1, lat2, lon2 float64) float64 {
	toRadians := func(value float64) float64 { return value * math.Pi / 180 }
	dLat := toRadians(lat2 - lat1)
	dLon := toRadians(lon2 - lon1)
	sinLat := math.Sin(dLat / 2)
	sinLon := math.Sin(dLon / 2)
	a := math.Pow(sinLat, 2) + math.Pow(sinLon, 2)*math.Cos(toRadians(lat1))*math.Cos(toRadians(lat2))
	c := 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
	return 6371.0 * c / 1000.0
}

func (a *Application) loginPlan(operation api.Operation, sample api.Sample) (api.OperationPlan, error) {
	username, password, err := hotelsupport.User(int(sample.Random % 501))
	if err != nil {
		return api.OperationPlan{}, err
	}
	if operation.Name == operationLoginInvalid {
		password += "-invalid"
	}
	return oneRequestPlan(operation, api.HTTPRequestSpec{
		Method: http.MethodGet,
		Path:   "/user",
		Query:  map[string]string{"username": username, "password": password},
	}, nil), nil
}

func (a *Application) reservationPlan(
	operation api.Operation,
	sample api.Sample,
	data *fixture,
) (api.OperationPlan, error) {
	if sample.Counter < 0 {
		return api.OperationPlan{}, fmt.Errorf("Hotel sample counter must be non-negative")
	}
	a.reservationMu.Lock()
	release := func() { a.reservationMu.Unlock() }

	// Sample.Counter restarts between warmup and measurement. The fixture-owned
	// ordinal persists across both phases. Pair each date with every hotel before
	// advancing to the next date, expanding the collision-free namespace without
	// consuming additional calendar range.
	ordinal := data.nextReservation.Add(1) - 1
	const reservationSlots = reservationDateBlockDays * 80
	if ordinal >= reservationSlots {
		release()
		return api.OperationPlan{}, fmt.Errorf(
			"Hotel trial exceeded its %d unique hotel/date reservation slots",
			reservationSlots,
		)
	}
	// The fixture-derived rotation changes the hotel-to-ordinal mapping between
	// independent trials even in the unlikely event that date blocks collide.
	hotelID := 1 + int((ordinal+data.hotelOffset)%80)
	capacity, err := hotelsupport.Capacity(hotelID)
	if err != nil {
		release()
		return api.OperationPlan{}, err
	}
	username, password, err := hotelsupport.User(int((sample.Random >> 9) % 501))
	if err != nil {
		release()
		return api.OperationPlan{}, err
	}
	inDate := data.dateBase.AddDate(0, 0, int(ordinal/80))
	outDate := inDate.AddDate(0, 0, 1)
	baseQuery := map[string]string{
		"inDate": inDate.Format(time.DateOnly), "outDate": outDate.Format(time.DateOnly),
		"hotelId":      strconv.Itoa(hotelID),
		"customerName": fmt.Sprintf("vibesys-%016x-%d", sample.Random, sample.Counter),
		"username":     username, "password": password,
	}
	request := func(number int) api.HTTPRequestSpec {
		query := make(map[string]string, len(baseQuery)+1)
		for key, value := range baseQuery {
			query[key] = value
		}
		query["number"] = strconv.Itoa(number)
		return api.HTTPRequestSpec{Method: http.MethodGet, Path: "/reservation", Query: query}
	}
	return api.OperationPlan{
		Invocations: []api.Invocation{
			{Target: operation.Target, Operation: operation.Name, Payload: request(capacity)},
			{Target: operation.Target, Operation: operation.Name, Payload: request(1)},
		},
		State: &operationState{kind: operation.Name, release: release},
	}, nil
}

func (a *Application) FinishOperation(plan api.OperationPlan) {
	state, ok := plan.State.(*operationState)
	if ok && state != nil && state.release != nil {
		state.release()
		state.release = nil
	}
}
