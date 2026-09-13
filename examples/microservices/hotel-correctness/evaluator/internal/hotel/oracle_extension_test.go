package hotel

import (
	"context"
	"errors"
	"math/rand"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"vibesys/microservice-evaluator/accuracy"
	"vibesys/microservice-evaluator/api"
)

// fakeService is a configurable stand-in for a Hotel candidate. Each knob
// reproduces one behavior observed in the pinned upstream DeathStarBench
// deployment, so the tests assert what the oracle does about a real defect
// rather than about an invented one.
type fakeService struct {
	t       *testing.T
	catalog map[string]profile

	mu    sync.Mutex
	model *sequentialModel

	// checkThenSetGap reproduces the upstream MakeReservation shape: the room
	// count is read, released, and only then written back, so simultaneous
	// requests all observe the same pre-burst count.
	checkThenSetGap time.Duration
	// cacheOnlyAvailability serves search from derived state that a restart
	// discards, as the upstream availability path does.
	cacheOnlyAvailability bool
	// dieOnUnknownHotel reproduces the upstream capacity lookup that calls
	// log.Panic when no seeded row matches.
	dieOnUnknownHotel bool
	// dropAcknowledgedWrites loses durable state across a restart.
	dropAcknowledgedWrites bool
	// chargeDegenerateRange consumes a night for a range enclosing none.
	chargeDegenerateRange bool
	// partialMultiNight applies the nights of a rejected span that had room.
	partialMultiNight bool

	availabilityLost bool
	dead             bool
}

func newFakeService(t *testing.T, catalog map[string]profile) *fakeService {
	t.Helper()
	model, err := newSequentialModel(catalog)
	if err != nil {
		t.Fatal(err)
	}
	return &fakeService{t: t, catalog: catalog, model: model}
}

func (s *fakeService) restart(context.Context) error {
	if err := s.crash(context.Background()); err != nil {
		return err
	}
	return s.start(context.Background())
}

func (s *fakeService) crash(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dead = true
	if s.cacheOnlyAvailability {
		s.availabilityLost = true
	}
	if s.dropAcknowledgedWrites {
		s.model.state.Reserved = make(map[string]int)
	}
	return nil
}

func (s *fakeService) start(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dead = false
	return nil
}

func (s *fakeService) Invoke(_ context.Context, invocation api.Invocation) api.ProtocolResult {
	spec, ok := invocation.Payload.(api.HTTPRequestSpec)
	if !ok {
		s.t.Fatalf("invocation payload=%T, want api.HTTPRequestSpec", invocation.Payload)
	}
	s.mu.Lock()
	dead := s.dead
	s.mu.Unlock()
	if dead {
		return api.ProtocolResult{
			TransportSuccess: false,
			ErrorCategory:    "connection_refused",
			ErrorMessage:     "fake service is not serving",
		}
	}
	switch spec.Path {
	case "/user":
		return httpResult(200, map[string]any{"message": s.login(spec.Query)})
	case "/reservation":
		return s.reserve(spec.Query)
	case "/hotels":
		return s.search(spec.Query)
	default:
		s.t.Fatalf("unexpected path %q", spec.Path)
		return api.ProtocolResult{}
	}
}

func (s *fakeService) login(query map[string]string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.model.login(query).Message
}

func (s *fakeService) reserve(query map[string]string) api.ProtocolResult {
	if _, err := capacityForHotel(query["hotelId"]); err != nil {
		if s.dieOnUnknownHotel {
			s.mu.Lock()
			s.dead = true
			s.mu.Unlock()
		}
		return api.ProtocolResult{
			TransportSuccess: false,
			ErrorCategory:    "internal_error",
			ErrorMessage:     err.Error(),
		}
	}
	nights, err := reservationNights(query)
	if err != nil {
		s.t.Fatalf("fake service could not parse dates: %v", err)
	}
	rooms, err := strconv.Atoi(query["number"])
	if err != nil {
		s.t.Fatalf("fake service could not parse room count: %v", err)
	}
	if len(nights) == 0 && s.chargeDegenerateRange {
		nights = []string{query["inDate"]}
	}

	admitted := s.admit(query["hotelId"], nights, rooms)
	if !admitted {
		return httpResult(200, map[string]any{"message": reservationFailure})
	}
	s.mu.Lock()
	message := reservationSuccess
	if !s.model.validCredentials(query) {
		message = loginFailure
	}
	s.mu.Unlock()
	return httpResult(200, map[string]any{"message": message})
}

// admit performs the capacity decision. With checkThenSetGap set, the read and
// the write are separated, which is exactly the upstream window that lets more
// requests through than there are rooms.
func (s *fakeService) admit(hotelID string, nights []string, rooms int) bool {
	capacity, err := capacityForHotel(hotelID)
	if err != nil {
		s.t.Fatal(err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	counts := make(map[string]int, len(nights))
	fits := true
	for _, date := range nights {
		count := s.model.state.Reserved[reservationStateKey(hotelID, date)]
		counts[date] = count
		if count+rooms > capacity {
			fits = false
		}
	}

	// Upstream releases the room count between the capacity test and the
	// write-back, so reproduce that window by dropping the lock across the
	// gap: the stale counts above then decide an admission that concurrent
	// requests have already invalidated. Without a gap the decision stays one
	// critical section, which is what a correctly serialized candidate does.
	// Sleeping while still holding the lock would only slow the fake down, and
	// unlocking without a gap would leave the "serialized" fake racy too.
	if s.checkThenSetGap > 0 {
		s.mu.Unlock()
		time.Sleep(s.checkThenSetGap)
		s.mu.Lock()
	}

	if !fits {
		if !s.partialMultiNight {
			return false
		}
		for _, date := range nights {
			if counts[date]+rooms <= capacity {
				s.model.state.Reserved[reservationStateKey(hotelID, date)] += rooms
			}
		}
		return false
	}
	for _, date := range nights {
		s.model.state.Reserved[reservationStateKey(hotelID, date)] += rooms
	}
	return true
}

func (s *fakeService) search(query map[string]string) api.ProtocolResult {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := sortedRateBackedIDs()
	if !s.availabilityLost {
		observation, err := s.model.search(query)
		if err != nil {
			s.t.Fatalf("fake service could not evaluate search: %v", err)
		}
		ids = observation.HotelIDs
	}
	features := make([]any, 0, len(ids))
	for _, id := range ids {
		item := s.catalog[id]
		features = append(features, map[string]any{
			"type": "Feature",
			"id":   item.id,
			"properties": map[string]any{
				"name": item.name, "phone_number": item.phone,
			},
			"geometry": map[string]any{
				"type": "Point", "coordinates": []any{item.lon, item.lat},
			},
		})
	}
	return httpResult(200, map[string]any{"type": "FeatureCollection", "features": features})
}

func testRandom(seed int64) *rand.Rand { return rand.New(rand.NewSource(seed)) }

func newFakeApplication(t *testing.T) (*Application, *fakeService, client) {
	t.Helper()
	catalog, err := seedProfiles()
	if err != nil {
		t.Fatal(err)
	}
	service := newFakeService(t, catalog)
	return &Application{catalog: catalog}, service, client{runtime: service, timeout: 5 * time.Second}
}

func requireProgramCounterexample(
	t *testing.T,
	err error,
) *accuracy.ProgramCounterexample[differentialAction, differentialObservation] {
	t.Helper()
	var counterexample *accuracy.ProgramCounterexample[differentialAction, differentialObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("error is not a replayable program counterexample: %v", err)
	}
	return counterexample
}

func TestLinearizableCapacityRejectsCheckThenSetOversell(t *testing.T) {
	application, service, c := newFakeApplication(t)
	service.checkThenSetGap = 50 * time.Millisecond
	_, err := application.verifyLinearizableCapacity(context.Background(), c, 91, 1, testRandom(1))
	counterexample := requireProgramCounterexample(t, err)

	if counterexample.SchemaVersion != accuracy.ProgramCounterexampleSchemaVersion ||
		counterexample.Program.ID != "hotel-linearizable-capacity-0" {
		t.Fatalf("counterexample metadata=%+v", counterexample)
	}
	if counterexample.Step != 1 || len(counterexample.Program.Steps) != 2 ||
		len(counterexample.Trace.Steps[1].Calls) < 2 {
		t.Fatalf("counterexample is not replayable: %+v", counterexample)
	}
	if strings.Contains(err.Error(), `"seed"`) {
		t.Fatal("counterexample disclosed the hidden replay seed")
	}
}

func TestLinearizableCapacityAcceptsSerializedCandidate(t *testing.T) {
	application, _, c := newFakeApplication(t)
	checks, err := application.verifyLinearizableCapacity(context.Background(), c, 91, 3, testRandom(1))
	if err != nil {
		t.Fatal(err)
	}
	if checks == 0 {
		t.Fatal("serialized candidate recorded no checks")
	}
}

func TestLinearizableCapacityGeneratorRespectsParallelBound(t *testing.T) {
	for remaining := 1; remaining <= 3; remaining++ {
		concurrency, err := linearizableConcurrency(remaining)
		if err != nil {
			t.Fatal(err)
		}
		if concurrency <= remaining || concurrency > accuracy.MaxParallelCalls {
			t.Fatalf(
				"remaining=%d concurrency=%d framework limit=%d",
				remaining,
				concurrency,
				accuracy.MaxParallelCalls,
			)
		}
	}
}

func TestConcurrentIsolationAcceptsDistinctHotelsAndRejectsSharedCapacity(t *testing.T) {
	application, _, c := newFakeApplication(t)
	if _, err := application.verifyConcurrentIsolation(context.Background(), c, 23, testRandom(2)); err != nil {
		t.Fatal(err)
	}

	// A candidate that shares one capacity counter across hotels rejects
	// requests that contend with nothing.
	shared, service, sharedClient := newFakeApplication(t)
	service.checkThenSetGap = 0
	sharedService := &sharedCapacityService{fakeService: service}
	sharedClient.runtime = sharedService
	_, err := shared.verifyConcurrentIsolation(context.Background(), sharedClient, 23, testRandom(2))
	counterexample := requireProgramCounterexample(t, err)
	if counterexample.Program.ID != "hotel-concurrent-isolation" ||
		counterexample.Step != 0 {
		t.Fatalf("counterexample=%+v", counterexample)
	}
}

func TestCrashRecoveryUsesLifecycleProgramAndCanonicalOracle(t *testing.T) {
	application, service, c := newFakeApplication(t)
	if _, err := application.verifyCrashRecovery(
		context.Background(), c, 31, testRandom(7), service.crash, service.start,
	); err != nil {
		t.Fatal(err)
	}

	lost, lostService, lostClient := newFakeApplication(t)
	lostService.dropAcknowledgedWrites = true
	_, err := lost.verifyCrashRecovery(
		context.Background(), lostClient, 31, testRandom(7), lostService.crash, lostService.start,
	)
	counterexample := requireProgramCounterexample(t, err)
	if counterexample.Program.ID != "hotel-crash-recovery" || counterexample.Step < 3 {
		t.Fatalf("counterexample=%+v", counterexample)
	}
}

// sharedCapacityService rewrites every reservation onto one hotel, modeling a
// candidate whose optimization collapsed per-hotel capacity into one counter.
type sharedCapacityService struct{ *fakeService }

func (s *sharedCapacityService) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	spec, ok := invocation.Payload.(api.HTTPRequestSpec)
	if ok && spec.Path == "/reservation" {
		rewritten := cloneQuery(spec.Query)
		rewritten["hotelId"] = "1"
		rewritten["number"] = strconv.Itoa(300)
		spec.Query = rewritten
		invocation.Payload = spec
	}
	return s.fakeService.Invoke(ctx, invocation)
}

func TestDurableStateAcceptsDurableCandidate(t *testing.T) {
	application, service, c := newFakeApplication(t)
	checks, err := application.verifyDurableState(
		context.Background(), c, 37, 2, testRandom(3), service.crash, service.start, true,
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks == 0 {
		t.Fatal("durable candidate recorded no checks")
	}
}

func TestDurableStateRejectsCacheOnlyAvailability(t *testing.T) {
	application, service, c := newFakeApplication(t)
	service.cacheOnlyAvailability = true
	_, err := application.verifyDurableState(
		context.Background(), c, 37, 2, testRandom(3), service.crash, service.start, true,
	)
	counterexample := requireProgramCounterexample(t, err)
	failing := counterexample.Program.Steps[counterexample.Step]
	if counterexample.Program.ID != "hotel-durable-state" ||
		failing.Call == nil ||
		failing.Call.Action.Kind != actionSearch {
		t.Fatalf("counterexample=%+v", counterexample)
	}

	// The same defect must not be reported when the workload has not opted in,
	// because the pinned reference implementation exhibits it.
	lenient, lenientService, lenientClient := newFakeApplication(t)
	lenientService.cacheOnlyAvailability = true
	if _, err := lenient.verifyDurableState(
		context.Background(), lenientClient, 37, 2, testRandom(3),
		lenientService.crash, lenientService.start, false,
	); err != nil {
		t.Fatalf("cache-only availability failed the default gate: %v", err)
	}
}

func TestDurableStateRejectsLostAcknowledgedReservation(t *testing.T) {
	application, service, c := newFakeApplication(t)
	service.dropAcknowledgedWrites = true
	_, err := application.verifyDurableState(
		context.Background(), c, 37, 2, testRandom(3), service.crash, service.start, false,
	)
	counterexample := requireProgramCounterexample(t, err)
	failing := counterexample.Program.Steps[counterexample.Step]
	if counterexample.Program.ID != "hotel-durable-state" ||
		failing.Call == nil ||
		failing.Call.Action.Kind != actionReserve {
		t.Fatalf("counterexample=%+v", counterexample)
	}
	if counterexample.Step < 4 || len(counterexample.Program.Steps) != counterexample.Step+1 {
		t.Fatalf("counterexample is not replayable: %+v", counterexample)
	}
}

func TestDegenerateDateRangesPinUpstreamBehavior(t *testing.T) {
	application, _, c := newFakeApplication(t)
	if _, err := application.verifyDegenerateDateRanges(context.Background(), c, 61, testRandom(4)); err != nil {
		t.Fatal(err)
	}

	charging, service, chargingClient := newFakeApplication(t)
	service.chargeDegenerateRange = true
	_, err := charging.verifyDegenerateDateRanges(context.Background(), chargingClient, 61, testRandom(4))
	if err == nil {
		t.Fatal("a candidate that charges an empty date range passed")
	}
	if !strings.Contains(err.Error(), "range") {
		t.Fatalf("rejection does not name the degenerate range: %v", err)
	}
}

func TestMultiNightAtomicityRejectsPartialApplication(t *testing.T) {
	application, _, c := newFakeApplication(t)
	if _, err := application.verifyMultiNightAtomicity(context.Background(), c, 73, 2, testRandom(5)); err != nil {
		t.Fatal(err)
	}

	partial, service, partialClient := newFakeApplication(t)
	service.partialMultiNight = true
	_, err := partial.verifyMultiNightAtomicity(context.Background(), partialClient, 73, 2, testRandom(5))
	if err == nil {
		t.Fatal("a candidate that partially applies a rejected span passed")
	}
	if !strings.Contains(err.Error(), "consumed flanking night") {
		t.Fatalf("unexpected rejection reason: %v", err)
	}
}

func TestEndpointLivenessRejectsFatalUnknownHotel(t *testing.T) {
	application, service, c := newFakeApplication(t)
	service.dieOnUnknownHotel = true
	_, err := application.verifyEndpointLiveness(context.Background(), c, 83, testRandom(6))
	if err == nil {
		t.Fatal("a candidate that stops serving after an unknown hotel passed")
	}
	if !strings.Contains(err.Error(), "stopped serving") {
		t.Fatalf("unexpected rejection reason: %v", err)
	}

	surviving, _, survivingClient := newFakeApplication(t)
	if _, err := surviving.verifyEndpointLiveness(
		context.Background(), survivingClient, 83, testRandom(6),
	); err != nil {
		t.Fatal(err)
	}
}
