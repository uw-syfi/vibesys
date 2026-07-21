package hotel

import (
	"context"
	"encoding/json"
	"math/rand"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

func TestCatalogIsIndependentCompleteAndExact(t *testing.T) {
	catalog, err := seedProfiles()
	if err != nil {
		t.Fatal(err)
	}
	if len(catalog) != 80 {
		t.Fatalf("catalog size=%d", len(catalog))
	}
	checks := map[string]profile{
		"1": {name: "Clift Hotel", phone: "(415) 775-4700", lat: 37.7867, lon: -122.4112},
		"7": {
			name: "St. Regis San Francisco", phone: "(415) 284-407",
			lat: 37.7835 + float32(7)/500.0*3, lon: -122.41 + float32(7)/500.0*4,
		},
		"80": {
			name: "St. Regis San Francisco", phone: "(415) 284-4080",
			lat: 37.7835 + float32(80)/500.0*3, lon: -122.41 + float32(80)/500.0*4,
		},
	}
	for id, want := range checks {
		got, ok := catalog[id]
		if !ok || got.name != want.name || got.phone != want.phone || got.lat != want.lat || got.lon != want.lon {
			t.Fatalf("catalog[%s]=%+v, want %+v", id, got, want)
		}
	}
	backed := rateBackedIDs()
	for _, id := range []string{"1", "2", "3", "9", "78"} {
		if _, ok := backed[id]; !ok {
			t.Fatalf("rate-backed ID %s missing", id)
		}
	}
	for _, id := range []string{"6", "7", "80"} {
		if _, ok := backed[id]; ok {
			t.Fatalf("non-rate-backed ID %s accepted", id)
		}
	}
}

func TestGeoJSONStrictSchemaIsOrderIndependent(t *testing.T) {
	raw := `{
		"features":[{
			"geometry":{"coordinates":[-122.4112,37.7867],"type":"Point"},
			"properties":{"phone_number":"(415) 775-4700","name":"Clift Hotel"},
			"id":"1","type":"Feature"
		}],"type":"FeatureCollection"
	}`
	decoded, err := httpcheck.DecodeJSON([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	features, err := decodeFeatureCollection(decoded)
	if err != nil {
		t.Fatal(err)
	}
	catalog, _ := seedProfiles()
	if err := validateProfiles(features, catalog, "test"); err != nil {
		t.Fatal(err)
	}
}

func TestGeoJSONRejectsSchemaValueAndUniquenessMutants(t *testing.T) {
	base := func() map[string]any {
		return map[string]any{
			"type": "FeatureCollection",
			"features": []any{map[string]any{
				"type": "Feature", "id": "1",
				"properties": map[string]any{"name": "Clift Hotel", "phone_number": "(415) 775-4700"},
				"geometry":   map[string]any{"type": "Point", "coordinates": []any{-122.4112, 37.7867}},
			}},
		}
	}
	tests := []struct {
		name   string
		mutate func(map[string]any)
		value  bool
	}{
		{"extra root field", func(root map[string]any) { root["extra"] = true }, false},
		{"wrong coordinate type", func(root map[string]any) {
			feature := root["features"].([]any)[0].(map[string]any)
			feature["geometry"].(map[string]any)["coordinates"] = []any{"-122", 37.7867}
		}, false},
		{"duplicate id", func(root map[string]any) {
			items := root["features"].([]any)
			clone := deepClone(t, items[0])
			root["features"] = append(items, clone)
		}, false},
		{"wrong seeded value", func(root map[string]any) {
			feature := root["features"].([]any)[0].(map[string]any)
			feature["properties"].(map[string]any)["name"] = "Impostor"
		}, true},
	}
	catalog, _ := seedProfiles()
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			root := base()
			test.mutate(root)
			features, err := decodeFeatureCollection(normalize(t, root))
			if test.value {
				if err != nil {
					t.Fatal(err)
				}
				err = validateProfiles(features, catalog, "mutant")
			}
			if err == nil {
				t.Fatal("mutant passed strict validation")
			}
		})
	}
}

func TestSearchRequiresExactRateBackedSetAndStableResponses(t *testing.T) {
	catalog, _ := seedProfiles()
	application := &Application{catalog: catalog}
	features := make(map[string]feature)
	for id := range rateBackedIDs() {
		features[id] = featureFromProfile(t, catalog[id])
	}
	if err := application.validateSearchSet(features); err != nil {
		t.Fatal(err)
	}
	if err := sameIDs(features, features); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(map[string]feature){
		"valid truncation": func(items map[string]feature) { delete(items, "78") },
		"non-rate extra":   func(items map[string]feature) { items["7"] = featureFromProfile(t, catalog["7"]) },
		"changed warm set": func(items map[string]feature) { delete(items, "9") },
	} {
		t.Run(name, func(t *testing.T) {
			candidate := cloneFeatures(features)
			mutate(candidate)
			var err error
			if name == "changed warm set" {
				err = sameIDs(features, candidate)
			} else {
				err = application.validateSearchSet(candidate)
			}
			if err == nil {
				t.Fatal("search mutant passed")
			}
		})
	}
}

func TestSearchFuzzingIsSeededVariedAndRepeated(t *testing.T) {
	catalog, _ := seedProfiles()
	features := make([]any, 0, len(rateBackedIDs()))
	for id := range rateBackedIDs() {
		features = append(features, featureFromProfile(t, catalog[id]).raw)
	}
	var requests []api.HTTPRequestSpec
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		requests = append(requests, invocation.Payload.(api.HTTPRequestSpec))
		return httpResult(200, map[string]any{"type": "FeatureCollection", "features": features})
	})
	application := &Application{catalog: catalog}
	checks, err := application.verifySearch(
		context.Background(),
		client{runtime: runtime, timeout: time.Second},
		rand.New(rand.NewSource(91)),
		4,
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks != 8 || len(requests) != 8 {
		t.Fatalf("checks=%d requests=%d, want 8", checks, len(requests))
	}
	seen := make(map[string]struct{})
	for index := 0; index < len(requests); index += 2 {
		if !reflect.DeepEqual(requests[index].Query, requests[index+1].Query) {
			t.Fatalf("case %d was not repeated exactly", index/2)
		}
		query := requests[index].Query
		seen[query["inDate"]+"/"+query["outDate"]+"/"+query["lat"]+"/"+query["lon"]] = struct{}{}
	}
	if len(seen) < 2 {
		t.Fatalf("fuzzing generated only %d distinct query", len(seen))
	}
}

func TestSearchAvailabilityConnectsAcknowledgedReservationsToReads(t *testing.T) {
	catalog, _ := seedProfiles()
	var requests []api.HTTPRequestSpec
	filledID := ""
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		requests = append(requests, spec)
		if spec.Path == "/reservation" {
			if filledID == "" {
				filledID = spec.Query["hotelId"]
				return httpResult(200, map[string]any{"message": reservationSuccess})
			}
			return httpResult(200, map[string]any{"message": reservationFailure})
		}
		features := make([]any, 0, len(rateBackedIDs()))
		for _, id := range sortedRateBackedIDs() {
			if len(requests) == 3 && id == filledID {
				continue
			}
			features = append(features, featureFromProfile(t, catalog[id]).raw)
		}
		return httpResult(200, map[string]any{"type": "FeatureCollection", "features": features})
	})
	application := &Application{catalog: catalog}
	checks, err := application.verifySearchAvailability(
		context.Background(),
		client{runtime: runtime, timeout: time.Second},
		37,
		rand.New(rand.NewSource(101)),
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks != 4 || len(requests) != 4 {
		t.Fatalf("checks=%d requests=%d, want 4", checks, len(requests))
	}
	if requests[0].Query["hotelId"] != filledID || requests[1].Query["number"] != "1" {
		t.Fatalf("reservation witness is not a fill/read-back pair: %+v", requests[:2])
	}
	if requests[2].Query["inDate"] != requests[0].Query["inDate"] {
		t.Fatal("first search did not query the filled date")
	}
	if requests[3].Query["inDate"] != requests[0].Query["outDate"] {
		t.Fatal("second search did not query the adjacent date")
	}
}

func TestApplicationContractAndReservationNamespace(t *testing.T) {
	workload := api.Workload{
		Load:    api.Load{TimeoutSeconds: 2, Seed: 19},
		Targets: []api.Target{{Name: "gateway", Protocol: "http", SessionPolicy: "reuse"}},
	}
	application, err := New(workload)
	if err != nil {
		t.Fatal(err)
	}
	if application.Name() != "hotel" {
		t.Fatalf("name=%q", application.Name())
	}
	policy := application.CasePolicy()
	if policy.MinimumCases != 4 || policy.RandomExtraCases != 3 {
		t.Fatalf("policy=%+v", policy)
	}
	properties := application.Properties()
	if len(properties) != 13 {
		t.Fatalf("properties=%v", properties)
	}
	for _, property := range properties {
		if property.Name == "crash_recovery" {
			if property.Required {
				t.Fatal("crash_recovery must remain optional without a lifecycle hook")
			}
			continue
		}
		if !property.Required {
			t.Fatalf("property %s is unexpectedly optional", property.Name)
		}
	}
	first := reservationDate(19)
	if !first.Equal(reservationDate(19)) || first.Equal(reservationDate(20)) {
		t.Fatal("reservation namespace is not deterministic and seed-separated")
	}
	if first.Year() < 3000 || first.Year() > 7999 || !first.After(time.Now()) {
		t.Fatalf("reservation date=%v is outside the collision-resistant future range", first)
	}
}

func TestClientRejectsNonExactMessagesAndNon400NegativeCases(t *testing.T) {
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Path == "/user" {
			return httpResult(200, map[string]any{"message": loginSuccess, "extra": true})
		}
		return httpResult(200, map[string]any{"message": "ok"})
	})
	c := client{runtime: runtime, timeout: time.Second}
	if err := c.exactMessage(context.Background(), "/user", nil, loginSuccess); err == nil || !strings.Contains(err.Error(), "fields") {
		t.Fatalf("message error=%v", err)
	}
	if err := c.badRequest(context.Background(), "/hotels", nil); err == nil {
		t.Fatal("HTTP 200 passed a negative case")
	}
}

func TestReservationCasesRandomizeHotelsAndExerciseEveryInvariant(t *testing.T) {
	const cases = 5
	var requests []api.HTTPRequestSpec
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		requests = append(requests, spec)
		messages := []string{
			reservationSuccess,
			reservationFailure,
			reservationFailure,
			reservationSuccess,
			reservationSuccess,
		}
		return httpResult(200, map[string]any{"message": messages[(len(requests)-1)%len(messages)]})
	})
	a := &Application{}
	checks, err := a.verifyReservations(
		context.Background(),
		client{runtime: runtime, timeout: time.Second},
		29,
		cases,
		rand.New(rand.NewSource(71)),
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks != cases*5 || len(requests) != cases*5 {
		t.Fatalf("checks=%d requests=%d, want %d", checks, len(requests), cases*5)
	}
	seenHotels := make(map[string]struct{})
	seenDates := make(map[string]struct{})
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		batch := requests[caseIndex*5 : caseIndex*5+5]
		primary := batch[0].Query["hotelId"]
		isolation := batch[4].Query["hotelId"]
		if primary == isolation {
			t.Fatalf("case %d reused hotel %s for isolation", caseIndex, primary)
		}
		for _, id := range []string{primary, isolation} {
			if _, duplicate := seenHotels[id]; duplicate {
				t.Fatalf("case %d reused randomized hotel %s", caseIndex, id)
			}
			seenHotels[id] = struct{}{}
		}
		exactDate := batch[0].Query["inDate"]
		adjacentDate := batch[2].Query["inDate"]
		for _, date := range []string{exactDate, adjacentDate} {
			if _, duplicate := seenDates[date]; duplicate {
				t.Fatalf("case %d reused date slot %s", caseIndex, date)
			}
			seenDates[date] = struct{}{}
		}
		if batch[1].Query["inDate"] != exactDate || batch[3].Query["inDate"] != adjacentDate || batch[4].Query["inDate"] != adjacentDate {
			t.Fatalf("case %d request dates do not exercise same-slot visibility/isolation", caseIndex)
		}
		numericID, err := strconv.Atoi(primary)
		if err != nil {
			t.Fatal(err)
		}
		capacity, err := hotelsupport.Capacity(numericID)
		if err != nil {
			t.Fatal(err)
		}
		if batch[0].Query["number"] != strconv.Itoa(capacity) ||
			batch[1].Query["number"] != "1" ||
			batch[2].Query["number"] != strconv.Itoa(capacity+1) ||
			batch[3].Query["number"] != "1" || batch[4].Query["number"] != "1" {
			t.Fatalf("case %d room sequence is not exact-cap/+1/over-cap/1/isolation: %+v", caseIndex, batch)
		}
	}
}

type runtimeFunc func(context.Context, api.Invocation) api.ProtocolResult

func (function runtimeFunc) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	return function(ctx, invocation)
}

func httpResult(status int, value any) api.ProtocolResult {
	body, _ := json.Marshal(value)
	return api.ProtocolResult{TransportSuccess: true, Payload: api.HTTPResponse{StatusCode: status, Body: body}}
}

func normalize(t *testing.T, value any) any {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := httpcheck.DecodeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	return decoded
}

func deepClone(t *testing.T, value any) any { return normalize(t, value) }

func featureFromProfile(t *testing.T, item profile) feature {
	t.Helper()
	raw := map[string]any{
		"type": "Feature", "id": item.id,
		"properties": map[string]any{"name": item.name, "phone_number": item.phone},
		"geometry":   map[string]any{"type": "Point", "coordinates": []any{item.lon, item.lat}},
	}
	decoded, err := decodeFeature(normalize(t, raw), 0)
	if err != nil {
		t.Fatal(err)
	}
	return decoded
}

func cloneFeatures(source map[string]feature) map[string]feature {
	result := make(map[string]feature, len(source))
	for id, item := range source {
		result[id] = item
	}
	return result
}
