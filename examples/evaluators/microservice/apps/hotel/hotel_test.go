package hotel

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

func TestNewValidatesHotelContract(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*api.Workload)
		want   string
	}{
		{"missing gateway", func(workload *api.Workload) { workload.Targets = nil }, "target named"},
		{"wrong protocol", func(workload *api.Workload) { workload.Targets[0].Protocol = "grpc" }, "must use HTTP"},
		{"non-reused session", func(workload *api.Workload) { workload.Targets[0].SessionPolicy = "new" }, "session_policy reuse"},
		{"zero timeout", func(workload *api.Workload) { workload.Load.TimeoutSeconds = 0 }, "timeout must be positive"},
		{"unknown config", func(workload *api.Workload) { workload.ApplicationConfig["rooms"] = int64(2) }, "unknown Hotel application_config"},
		{"unknown operation", func(workload *api.Workload) { workload.Operations[0].Name = "search" }, "unknown Hotel operation"},
		{"wrong target", func(workload *api.Workload) { workload.Operations[0].Target = "search" }, "must target"},
		{"declarative request", func(workload *api.Workload) { workload.Operations[0].HTTP = &api.HTTPRequestSpec{} }, "must not declare"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			workload := hotelWorkload(operationSearch)
			test.mutate(&workload)
			if _, err := New(workload); err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("New() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestApplicationExposesSharedPreflight(t *testing.T) {
	applicationValue, err := New(hotelWorkload(operationLoginValid))
	if err != nil {
		t.Fatal(err)
	}
	preflight, ok := applicationValue.(api.PreflightApplication)
	if !ok {
		t.Fatal("Hotel benchmark does not implement api.PreflightApplication")
	}
	if len(preflight.ReadinessProbes()) != 3 || len(preflight.PreflightProbes()) != 2 {
		t.Fatalf("unexpected shared preflight cardinality: readiness=%d protocol=%d",
			len(preflight.ReadinessProbes()), len(preflight.PreflightProbes()))
	}
}

func TestBuildOperationCreatesDeterministicCanonicalQueries(t *testing.T) {
	tests := []struct {
		operation string
		path      string
		require   string
		steps     int
	}{
		{operationSearch, "/hotels", "", 1},
		{operationRecommendDistance, "/recommendations", "dis", 1},
		{operationRecommendRate, "/recommendations", "rate", 1},
		{operationRecommendPrice, "/recommendations", "price", 1},
		{operationLoginValid, "/user", "", 1},
		{operationLoginInvalid, "/user", "", 1},
		{operationReserveCapacity, "/reservation", "", 2},
	}
	for _, test := range tests {
		t.Run(test.operation, func(t *testing.T) {
			applicationValue, err := New(hotelWorkload(test.operation))
			if err != nil {
				t.Fatal(err)
			}
			application := applicationValue.(*Application)
			prepared, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 2, FixtureSeed: 99})
			if err != nil {
				t.Fatal(err)
			}
			operation := api.Operation{Name: test.operation, Target: hotelsupport.GatewayTarget}
			plan, err := application.BuildOperation(operation, api.Sample{Counter: 7, Random: 12345}, prepared)
			if err != nil {
				t.Fatal(err)
			}
			defer application.FinishOperation(plan)
			if len(plan.Invocations) != test.steps {
				t.Fatalf("invocations = %d, want %d", len(plan.Invocations), test.steps)
			}
			request := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
			if request.Method != http.MethodGet || request.Path != test.path || len(request.Form) != 0 || request.Body != "" {
				t.Fatalf("unexpected canonical request: %+v", request)
			}
			if test.operation == operationSearch {
				assertCanonicalSearchQuery(t, request.Query)
			}
			if test.require != "" {
				assertCanonicalRecommendationQuery(t, request.Query, test.require)
			}
			if test.operation == operationLoginValid || test.operation == operationLoginInvalid {
				username, password, userErr := hotelsupport.User(int(uint64(12345) % 501))
				if userErr != nil {
					t.Fatal(userErr)
				}
				if test.operation == operationLoginInvalid {
					password += "-invalid"
				}
				if request.Query["username"] != username || request.Query["password"] != password {
					t.Fatalf("login query = %v", request.Query)
				}
			}
			if test.operation == operationReserveCapacity {
				first := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
				second := plan.Invocations[1].Payload.(api.HTTPRequestSpec)
				hotelID, _ := strconv.Atoi(first.Query["hotelId"])
				capacity, _ := hotelsupport.Capacity(hotelID)
				if first.Query["number"] != strconv.Itoa(capacity) || second.Query["number"] != "1" {
					t.Fatalf("capacity sequence = %q then %q, capacity %d", first.Query["number"], second.Query["number"], capacity)
				}
				if first.Query["inDate"] != second.Query["inDate"] || first.Query["outDate"] != second.Query["outDate"] {
					t.Fatalf("capacity steps use different dates: %v then %v", first.Query, second.Query)
				}
			}
		})
	}
}

func TestFuzzedCanonicalQueriesAreDeterministicAndVariable(t *testing.T) {
	const sampleRandom = uint64(12345)
	wantSearch := map[string]string{
		"inDate": "2015-04-23", "outDate": "2015-04-24",
		"lat": "38.1383", "lon": "-121.9387",
	}
	if got := searchRequest(sampleRandom).Query; !reflect.DeepEqual(got, wantSearch) {
		t.Fatalf("search query for random word %d = %v, want %v", sampleRandom, got, wantSearch)
	}
	wantRecommendation := map[string]string{
		"lat": "38.0020", "lon": "-121.9980", "require": "dis", "locale": "en",
	}
	first, _, _ := recommendationRequest(operationRecommendDistance, sampleRandom)
	second, _, _ := recommendationRequest(operationRecommendDistance, sampleRandom)
	if !reflect.DeepEqual(first.Query, wantRecommendation) || !reflect.DeepEqual(first, second) {
		t.Fatalf("distance query is not deterministic: first=%v second=%v want=%v", first.Query, second.Query, wantRecommendation)
	}

	dates := make(map[string]struct{})
	locations := make(map[string]struct{})
	distanceWinners := make(map[string]struct{})
	locales := make(map[bool]struct{})
	for random := uint64(0); random < 4096; random++ {
		search := searchRequest(random)
		assertCanonicalSearchQuery(t, search.Query)
		_, hasLocale := search.Query["locale"]
		locales[hasLocale] = struct{}{}
		dates[search.Query["inDate"]+"/"+search.Query["outDate"]] = struct{}{}
		locations[search.Query["lat"]+"/"+search.Query["lon"]] = struct{}{}

		recommendation, lat, lon := recommendationRequest(operationRecommendDistance, random)
		assertCanonicalRecommendationQuery(t, recommendation.Query, "dis")
		distanceWinners[strings.Join(nearestRecommendationIDs(lat, lon), ",")] = struct{}{}
	}
	if len(dates) < 50 || len(locations) < 3000 || len(distanceWinners) < 20 || len(locales) != 2 {
		t.Fatalf("insufficient fuzz diversity: dates=%d locations=%d distance winners=%d locale forms=%d", len(dates), len(locations), len(distanceWinners), len(locales))
	}
}

func TestDistanceRecommendationOracleCoversFullCatalog(t *testing.T) {
	for number := 1; number <= 80; number++ {
		id := strconv.Itoa(number)
		item, err := profileForID(id)
		if err != nil {
			t.Fatal(err)
		}
		if got := nearestRecommendationIDs(item.recommendLat, item.recommendLon); !reflect.DeepEqual(got, []string{id}) {
			t.Fatalf("nearest hotel at catalog coordinate for %s = %v, want [%s]", id, got, id)
		}
	}

	request, lat, lon := recommendationRequest(operationRecommendDistance, 12345)
	expected := nearestRecommendationIDs(lat, lon)
	wrong := "1"
	if expected[0] == wrong {
		wrong = "2"
	}
	state := &operationState{kind: operationRecommendDistance, expectedIDs: expected}
	plan := api.OperationPlan{State: state, Invocations: []api.Invocation{{Payload: request}}}
	application := &Application{}
	result := httpJSONResult(geoJSON([]map[string]any{feature(wrong)}))
	validation := application.ValidateOperation(
		api.Operation{Name: operationRecommendDistance, Target: hotelsupport.GatewayTarget}, plan, []api.ProtocolResult{result},
	)
	if validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("wrong distance winner unexpectedly passed: expected=%v wrong=%s validation=%+v", expected, wrong, validation)
	}
}

func assertCanonicalSearchQuery(t *testing.T, query map[string]string) {
	t.Helper()
	if err := validateCanonicalSearchQuery(query); err != nil {
		t.Fatal(err)
	}
}

func validateCanonicalSearchQuery(query map[string]string) error {
	locale, hasLocale := query["locale"]
	if len(query) != 4 && len(query) != 5 || hasLocale && locale != "en" || len(query) == 5 && !hasLocale {
		return fmt.Errorf("search query has non-canonical fields: %v", query)
	}
	inDate, err := time.Parse(time.DateOnly, query["inDate"])
	if err != nil {
		return fmt.Errorf("invalid search arrival %q: %w", query["inDate"], err)
	}
	outDate, err := time.Parse(time.DateOnly, query["outDate"])
	if err != nil {
		return fmt.Errorf("invalid search departure %q: %w", query["outDate"], err)
	}
	if inDate.Year() != 2015 || inDate.Month() != time.April ||
		inDate.Day() < searchArrivalFirstDay || inDate.Day() > searchArrivalLastDay {
		return fmt.Errorf("search arrival %s lies outside official range", inDate.Format(time.DateOnly))
	}
	if outDate.Year() != 2015 || outDate.Month() != time.April ||
		outDate.Day() <= inDate.Day() || outDate.Day() > searchDepartureLastDay {
		return fmt.Errorf("search departure %s is not after arrival or lies outside official range", outDate.Format(time.DateOnly))
	}
	lat, lon, err := parseCanonicalLocation(query)
	if err != nil {
		return err
	}
	for id := 20; id <= 60; id++ {
		anchor, profileErr := profileForID(strconv.Itoa(id))
		if profileErr != nil {
			return profileErr
		}
		if math.Abs(lat-anchor.recommendLat) <= 0.0010000001 && math.Abs(lon-anchor.recommendLon) <= 0.0010000001 {
			return nil
		}
	}
	return fmt.Errorf("search location lat=%q lon=%q is not within the seeded anchor jitter window", query["lat"], query["lon"])
}

func assertCanonicalRecommendationQuery(t *testing.T, query map[string]string, require string) {
	t.Helper()
	if err := validateCanonicalRecommendationQuery(query, require); err != nil {
		t.Fatal(err)
	}
}

func validateCanonicalRecommendationQuery(query map[string]string, require string) error {
	locale, hasLocale := query["locale"]
	if len(query) != 3 && len(query) != 4 || hasLocale && locale != "en" || len(query) == 4 && !hasLocale || query["require"] != require {
		return fmt.Errorf("recommendation query has non-canonical fields: %v", query)
	}
	lat, lon, err := parseCanonicalLocation(query)
	if err != nil {
		return err
	}
	latStep := math.Round((lat - 37.7830) * 1000)
	lonStep := math.Round((lon - (-122.2520)) * 1000)
	if math.Abs(lat-(37.7830+latStep/1000)) > 1e-9 || latStep < 0 || latStep >= latitudeChoices {
		return fmt.Errorf("latitude %s lies outside official discrete range", query["lat"])
	}
	if math.Abs(lon-(-122.2520+lonStep/1000)) > 1e-9 || lonStep < 0 || lonStep >= longitudeChoices {
		return fmt.Errorf("longitude %s lies outside official discrete range", query["lon"])
	}
	return nil
}

func parseCanonicalLocation(query map[string]string) (float64, float64, error) {
	lat, latErr := strconv.ParseFloat(query["lat"], 64)
	lon, lonErr := strconv.ParseFloat(query["lon"], 64)
	if latErr != nil || lonErr != nil || formatCoordinate(lat) != query["lat"] || formatCoordinate(lon) != query["lon"] {
		return 0, 0, fmt.Errorf("location is not canonical: lat=%q lon=%q", query["lat"], query["lon"])
	}
	if lat < 37.7830 || lat > 38.2640 {
		return 0, 0, fmt.Errorf("latitude %s lies outside official range", query["lat"])
	}
	if lon < -122.2520 || lon > -121.9270 {
		return 0, 0, fmt.Errorf("longitude %s lies outside official range", query["lon"])
	}
	return lat, lon, nil
}

func TestGeoJSONValidationIsStrictAndOrderInsensitive(t *testing.T) {
	valid := []map[string]any{feature("3"), feature("1"), feature("2")}
	if validation := validateGeoJSON(httpJSONResult(geoJSON(valid)), []string{"1", "2", "3"}); !validation.Success {
		t.Fatalf("valid shuffled GeoJSON failed: %+v", validation)
	}

	tests := []struct {
		name     string
		features []map[string]any
		want     string
	}{
		{"omitted ID", []map[string]any{feature("1"), feature("2")}, "response_value"},
		{"duplicate ID", []map[string]any{feature("1"), feature("1"), feature("2")}, "response_value"},
		{"wrong exact set", []map[string]any{feature("1"), feature("2"), feature("9")}, "response_value"},
		{"unknown catalog ID", []map[string]any{feature("1"), feature("2"), rawFeature("81")}, "response_schema"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			validation := validateGeoJSON(httpJSONResult(geoJSON(test.features)), []string{"1", "2", "3"})
			if validation.Success || validation.ErrorCategory != test.want {
				t.Fatalf("adversarial GeoJSON unexpectedly passed: %+v", validation)
			}
		})
	}

	t.Run("extra root field", func(t *testing.T) {
		body := geoJSON(valid)
		body["status"] = "ok"
		validation := validateGeoJSON(httpJSONResult(body), []string{"1", "2", "3"})
		if validation.Success || validation.ErrorCategory != "response_schema" {
			t.Fatalf("extra root field unexpectedly passed: %+v", validation)
		}
	})
	t.Run("wrong catalog value", func(t *testing.T) {
		corrupt := feature("1")
		corrupt["properties"].(map[string]any)["phone_number"] = "(000) 000-0000"
		validation := validateGeoJSON(httpJSONResult(geoJSON([]map[string]any{corrupt})), []string{"1"})
		if validation.Success || validation.ErrorCategory != "response_schema" {
			t.Fatalf("wrong catalog property unexpectedly passed: %+v", validation)
		}
	})
	t.Run("wrong coordinate", func(t *testing.T) {
		corrupt := feature("1")
		corrupt["geometry"].(map[string]any)["coordinates"] = []float32{-122.0, 37.0}
		validation := validateGeoJSON(httpJSONResult(geoJSON([]map[string]any{corrupt})), []string{"1"})
		if validation.Success || validation.ErrorCategory != "response_schema" {
			t.Fatalf("wrong coordinates unexpectedly passed: %+v", validation)
		}
	})
	t.Run("extra feature field", func(t *testing.T) {
		corrupt := feature("1")
		corrupt["distance"] = 0
		validation := validateGeoJSON(httpJSONResult(geoJSON([]map[string]any{corrupt})), []string{"1"})
		if validation.Success || validation.ErrorCategory != "response_schema" {
			t.Fatalf("extra feature field unexpectedly passed: %+v", validation)
		}
	})
	t.Run("search requires exact complete rate-backed set", func(t *testing.T) {
		ids := rateBackedHotelIDs()
		features := make([]map[string]any, 0, len(ids))
		for index := len(ids) - 1; index >= 0; index-- {
			features = append(features, feature(ids[index]))
		}
		validation := validateGeoJSON(httpJSONResult(geoJSON(features)), ids)
		if !validation.Success {
			t.Fatalf("27-feature reference search response failed: %+v", validation)
		}
		for mutationIndex := range features {
			mutated := append([]map[string]any(nil), features...)
			mutated = append(mutated[:mutationIndex], mutated[mutationIndex+1:]...)
			if validation := validateGeoJSON(httpJSONResult(geoJSON(mutated)), ids); validation.Success {
				t.Fatalf("search response with rate-backed ID %q removed unexpectedly passed", ids[len(ids)-1-mutationIndex])
			}
		}
	})
}

func TestMessageValidationRequiresExactSchemaAndText(t *testing.T) {
	if validation := validateMessage(httpJSONResult(map[string]any{"message": loginSuccess}), loginSuccess); !validation.Success {
		t.Fatalf("valid message failed: %+v", validation)
	}
	for _, body := range []any{
		map[string]any{"message": loginSuccess + " "},
		map[string]any{"message": loginSuccess, "ok": true},
		map[string]any{"message": true},
		[]any{loginSuccess},
	} {
		if validation := validateMessage(httpJSONResult(body), loginSuccess); validation.Success {
			t.Fatalf("non-exact message body unexpectedly passed: %v", body)
		}
	}
}

func TestReservationPlansSerializeAndUseUniqueHotelDateSlots(t *testing.T) {
	application := &Application{}
	data := &fixture{dateBase: time.Date(2050, time.January, 1, 12, 0, 0, 0, time.UTC)}
	operation := api.Operation{Name: operationReserveCapacity, Target: hotelsupport.GatewayTarget}
	first, err := application.BuildOperation(operation, api.Sample{Counter: 1, Random: 1}, data)
	if err != nil {
		t.Fatal(err)
	}

	type outcome struct {
		plan api.OperationPlan
		err  error
	}
	ready := make(chan outcome, 1)
	go func() {
		// Counter deliberately repeats, as it does at the warmup/measurement
		// boundary. The fixture ordinal must still allocate a new hotel/date slot.
		plan, buildErr := application.BuildOperation(operation, api.Sample{Counter: 1, Random: 1}, data)
		ready <- outcome{plan: plan, err: buildErr}
	}()
	select {
	case result := <-ready:
		if result.err == nil {
			application.FinishOperation(result.plan)
		}
		t.Fatal("conflicting reservation plan was not serialized")
	case <-time.After(30 * time.Millisecond):
	}
	firstQuery := first.Invocations[0].Payload.(api.HTTPRequestSpec).Query
	application.FinishOperation(first)
	application.FinishOperation(first)
	select {
	case result := <-ready:
		if result.err != nil {
			t.Fatal(result.err)
		}
		secondQuery := result.plan.Invocations[0].Payload.(api.HTTPRequestSpec).Query
		application.FinishOperation(result.plan)
		if firstQuery["inDate"] == secondQuery["inDate"] && firstQuery["hotelId"] == secondQuery["hotelId"] {
			t.Fatalf("distinct sequence counters reused reservation slot hotel=%s date=%s", firstQuery["hotelId"], firstQuery["inDate"])
		}
	case <-time.After(time.Second):
		t.Fatal("reservation plan did not acquire lock after FinishOperation")
	}
}

func TestFixtureDateNamespaceIsDeterministicAndSeedDifferentiated(t *testing.T) {
	application := &Application{}
	firstValue, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 2, FixtureSeed: 99})
	if err != nil {
		t.Fatal(err)
	}
	sameValue, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 2, FixtureSeed: 99})
	if err != nil {
		t.Fatal(err)
	}
	differentSeedValue, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 2, FixtureSeed: 100})
	if err != nil {
		t.Fatal(err)
	}
	differentTrialValue, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 3, FixtureSeed: 99})
	if err != nil {
		t.Fatal(err)
	}
	first := firstValue.(*fixture).dateBase
	if first.Year() < 3000 || first.Year() >= 7000 {
		t.Fatalf("namespace date %s lies outside [3000, 7000)", first.Format(time.DateOnly))
	}
	if first != sameValue.(*fixture).dateBase {
		t.Fatal("same trial context produced a different date namespace")
	}
	if first == differentSeedValue.(*fixture).dateBase || first == differentTrialValue.(*fixture).dateBase {
		t.Fatalf("known distinct trial contexts collided at %s", first.Format(time.DateOnly))
	}
	days := daysBeforeYear(first.Year()) - daysBeforeYear(3000) + first.YearDay() - 1
	if days%reservationDateBlockDays != 0 {
		t.Fatalf("namespace date %s is not aligned to a %d-day block", first.Format(time.DateOnly), reservationDateBlockDays)
	}
}

func daysBeforeYear(year int) int {
	previous := year - 1
	return previous*365 + previous/4 - previous/100 + previous/400
}

func TestAllOperationsAgainstHTTPFake(t *testing.T) {
	operations := []string{
		operationSearch, operationRecommendDistance, operationRecommendRate, operationRecommendPrice,
		operationLoginValid, operationLoginInvalid, operationReserveCapacity,
	}
	workload := hotelWorkload(operations...)
	applicationValue, err := New(workload)
	if err != nil {
		t.Fatal(err)
	}
	application := applicationValue.(*Application)
	prepared, err := application.Prepare(context.Background(), nil, api.TrialContext{Index: 3, FixtureSeed: 42})
	if err != nil {
		t.Fatal(err)
	}
	target := newFakeHotel(t)
	runtime := &handlerRuntime{handler: target}
	for index, name := range operations {
		t.Run(name, func(t *testing.T) {
			operation := api.Operation{Name: name, Target: hotelsupport.GatewayTarget}
			plan, buildErr := application.BuildOperation(
				operation, api.Sample{Counter: int64(index + 1), Random: uint64(9182 + index)}, prepared,
			)
			if buildErr != nil {
				t.Fatal(buildErr)
			}
			defer application.FinishOperation(plan)
			results := make([]api.ProtocolResult, len(plan.Invocations))
			for resultIndex, invocation := range plan.Invocations {
				results[resultIndex] = runtime.Invoke(context.Background(), invocation)
			}
			if validation := application.ValidateOperation(operation, plan, results); !validation.Success {
				t.Fatalf("validation failed: %+v", validation)
			}
		})
	}
	if got, want := len(runtime.requests), len(operations)+1; got != want {
		t.Fatalf("HTTP requests = %d, want %d (reservation has two steps)", got, want)
	}
}

type fakeHotel struct {
	t            *testing.T
	mu           sync.Mutex
	reservations map[string]int
}

func newFakeHotel(t *testing.T) http.Handler {
	fake := &fakeHotel{t: t, reservations: make(map[string]int)}
	mux := http.NewServeMux()
	mux.HandleFunc("/hotels", fake.hotels)
	mux.HandleFunc("/recommendations", fake.recommendations)
	mux.HandleFunc("/user", fake.user)
	mux.HandleFunc("/reservation", fake.reservation)
	return mux
}

func (f *fakeHotel) hotels(writer http.ResponseWriter, request *http.Request) {
	query := singleQuery(request.URL.Query())
	if err := validateCanonicalSearchQuery(query); err != nil {
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	ids := rateBackedHotelIDs()
	features := make([]map[string]any, 0, len(ids))
	for index := len(ids) - 1; index >= 0; index-- {
		features = append(features, feature(ids[index]))
	}
	writeJSON(writer, geoJSON(features))
}

func (f *fakeHotel) recommendations(writer http.ResponseWriter, request *http.Request) {
	require := request.URL.Query().Get("require")
	operation := map[string]string{
		"dis": operationRecommendDistance, "rate": operationRecommendRate, "price": operationRecommendPrice,
	}[require]
	if operation == "" {
		http.Error(writer, "bad require", http.StatusBadRequest)
		return
	}
	query := singleQuery(request.URL.Query())
	if err := validateCanonicalRecommendationQuery(query, require); err != nil {
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	ids := append([]string(nil), expectedRecommendationIDs[operation]...)
	if operation == operationRecommendDistance {
		lat, _ := strconv.ParseFloat(query["lat"], 64)
		lon, _ := strconv.ParseFloat(query["lon"], 64)
		ids = nearestRecommendationIDs(lat, lon)
	}
	sort.Sort(sort.Reverse(sort.StringSlice(ids)))
	features := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		features = append(features, feature(id))
	}
	writeJSON(writer, geoJSON(features))
}

func (f *fakeHotel) user(writer http.ResponseWriter, request *http.Request) {
	valid := validCredentials(request.URL.Query().Get("username"), request.URL.Query().Get("password"))
	message := loginFailure
	if valid {
		message = loginSuccess
	}
	writeJSON(writer, map[string]any{"message": message})
}

func (f *fakeHotel) reservation(writer http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	if !validCredentials(query.Get("username"), query.Get("password")) {
		writeJSON(writer, map[string]any{"message": loginFailure})
		return
	}
	hotelID, idErr := strconv.Atoi(query.Get("hotelId"))
	number, numberErr := strconv.Atoi(query.Get("number"))
	capacity, capacityErr := hotelsupport.Capacity(hotelID)
	if idErr != nil || numberErr != nil || capacityErr != nil || query.Get("inDate") == "" || query.Get("outDate") == "" || query.Get("customerName") == "" {
		http.Error(writer, "invalid reservation query", http.StatusBadRequest)
		return
	}
	key := fmt.Sprintf("%d/%s/%s", hotelID, query.Get("inDate"), query.Get("outDate"))
	f.mu.Lock()
	defer f.mu.Unlock()
	message := reservationFailure
	if f.reservations[key]+number <= capacity {
		f.reservations[key] += number
		message = reservationSuccess
	}
	writeJSON(writer, map[string]any{"message": message})
}

func validCredentials(username, password string) bool {
	for index := 0; index <= 500; index++ {
		wantUsername, wantPassword, err := hotelsupport.User(index)
		if err != nil {
			panic(err)
		}
		if username == wantUsername {
			return password == wantPassword
		}
	}
	return false
}

type handlerRuntime struct {
	handler  http.Handler
	requests []api.HTTPRequestSpec
}

func (r *handlerRuntime) Invoke(_ context.Context, invocation api.Invocation) api.ProtocolResult {
	request, ok := invocation.Payload.(api.HTTPRequestSpec)
	if !ok {
		return api.ProtocolResult{ErrorCategory: "invalid_request", ErrorMessage: fmt.Sprintf("got %T", invocation.Payload)}
	}
	if invocation.Target != hotelsupport.GatewayTarget {
		return api.ProtocolResult{ErrorCategory: "invalid_target", ErrorMessage: invocation.Target}
	}
	r.requests = append(r.requests, request)
	requestURL := "http://hotel.test" + request.Path
	if len(request.Query) != 0 {
		query := make(url.Values, len(request.Query))
		for key, value := range request.Query {
			query.Set(key, value)
		}
		requestURL += "?" + query.Encode()
	}
	httpRequest := httptest.NewRequest(request.Method, requestURL, nil)
	recorder := httptest.NewRecorder()
	r.handler.ServeHTTP(recorder, httpRequest)
	return api.ProtocolResult{
		TransportSuccess: true,
		NativeStatus:     recorder.Result().Status,
		Payload: api.HTTPResponse{
			StatusCode: recorder.Code,
			Body:       append([]byte(nil), recorder.Body.Bytes()...),
		},
	}
}

func hotelWorkload(operations ...string) api.Workload {
	values := make([]api.Operation, len(operations))
	for index, name := range operations {
		values[index] = api.Operation{Name: name, Target: hotelsupport.GatewayTarget}
	}
	return api.Workload{
		Application: "hotel",
		Load: api.Load{
			TimeoutSeconds: 1, Repetitions: 1, Seed: 7,
		},
		Targets: []api.Target{{
			Name: hotelsupport.GatewayTarget, Protocol: "http", Address: "http://hotel.test", SessionPolicy: "reuse",
		}},
		Operations:        values,
		ApplicationConfig: map[string]any{},
	}
}

func feature(id string) map[string]any {
	item, err := profileForID(id)
	if err != nil {
		panic(err)
	}
	return map[string]any{
		"type": "Feature",
		"id":   item.id,
		"properties": map[string]any{
			"name": item.name, "phone_number": item.phoneNumber,
		},
		"geometry": map[string]any{
			"type": "Point", "coordinates": []float32{item.lon, item.lat},
		},
	}
}

func rawFeature(id string) map[string]any {
	return map[string]any{
		"type": "Feature", "id": id,
		"properties": map[string]any{"name": "unknown", "phone_number": "unknown"},
		"geometry":   map[string]any{"type": "Point", "coordinates": []float32{0, 0}},
	}
}

func geoJSON(features []map[string]any) map[string]any {
	return map[string]any{"type": "FeatureCollection", "features": features}
}

func httpJSONResult(value any) api.ProtocolResult {
	body, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return api.ProtocolResult{
		TransportSuccess: true,
		Payload:          api.HTTPResponse{StatusCode: http.StatusOK, Body: body},
	}
}

func writeJSON(writer http.ResponseWriter, value any) {
	writer.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(writer).Encode(value); err != nil {
		panic(err)
	}
}

func singleQuery(values url.Values) map[string]string {
	result := make(map[string]string, len(values))
	for key, entries := range values {
		if len(entries) == 1 {
			result[key] = entries[0]
		}
	}
	return result
}
