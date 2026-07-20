package trainticket

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
)

func TestMakeRecordsIsDeterministicAndConnectedWithoutSeedCatalogDependency(t *testing.T) {
	first := makeRecords("namespace", 42, 3)
	second := makeRecords("namespace", 42, 3)
	if !reflect.DeepEqual(first, second) {
		t.Fatal("same namespace and seed produced different records")
	}
	for index := range first {
		item := &first[index]
		if item.route.StartStationID != item.stationA.ID || item.route.TerminalStationID != item.stationB.ID {
			t.Fatalf("record %d route does not connect its generated stations", index)
		}
		if item.price.RouteID != item.route.ID || item.trip.RouteID != item.route.ID {
			t.Fatalf("record %d price/trip does not reference its route", index)
		}
		if item.price.TrainType != item.train.ID || item.trip.TrainTypeID != item.train.ID {
			t.Fatalf("record %d price/trip does not reference its train", index)
		}
	}
}

func TestAdminTokenUsesRandomModeNeutralIdentity(t *testing.T) {
	first := makeAdminToken(time.Unix(1_000, 0))
	second := makeAdminToken(time.Unix(1_000, 0))
	if first == second {
		t.Fatal("admin token identity was reused")
	}
	parts := strings.Split(first, ".")
	if len(parts) != 3 {
		t.Fatalf("token has %d parts, want 3", len(parts))
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		t.Fatal(err)
	}
	identity, ok := claims["sub"].(string)
	if !ok || claims["id"] != identity || len(identity) != 24 {
		t.Fatalf("unexpected identity claims: %+v", claims)
	}
	if strings.Contains(identity, "checker") || strings.Contains(identity, "benchmark") {
		t.Fatalf("mode-specific identity leaked in %q", identity)
	}
}

func TestNewRejectsUnknownApplicationConfiguration(t *testing.T) {
	workload := trainTicketWorkload()
	workload.ApplicationConfig = map[string]any{"recrods": int64(2)}
	if _, err := New(workload); err == nil {
		t.Fatal("misspelled application configuration was accepted")
	}
}

func TestUpdateReadPlanCommitsAcknowledgedValueAndDetectsStaleRead(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "update_read_config", Target: "config"}
	plan, err := application.BuildOperation(operation, api.Sample{Counter: 3, Random: 9}, data)
	if err != nil {
		t.Fatal(err)
	}
	state := plan.State.(*operationState)
	want := state.expectations[1].expected.(configEntity)
	stale := data.records[1].config
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusOK, 1, nil),
		httpResult(http.StatusOK, 1, stale),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "read_your_write" {
		t.Fatalf("stale read unexpectedly passed: %+v", validation)
	}
	if data.records[1].config != want {
		t.Fatalf("acknowledged write was not committed to the oracle: got %+v, want %+v", data.records[1].config, want)
	}
}

func TestReadPlansOnSameFixtureDoNotSerialize(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "read_config", Target: "config"}
	first, err := application.BuildOperation(operation, api.Sample{Random: 0}, data)
	if err != nil {
		t.Fatal(err)
	}
	defer application.FinishOperation(first)

	secondPlan := make(chan api.OperationPlan, 1)
	secondError := make(chan error, 1)
	go func() {
		plan, buildErr := application.BuildOperation(operation, api.Sample{Random: 0}, data)
		if buildErr != nil {
			secondError <- buildErr
			return
		}
		secondPlan <- plan
	}()

	select {
	case plan := <-secondPlan:
		application.FinishOperation(plan)
	case buildErr := <-secondError:
		t.Fatal(buildErr)
	case <-time.After(250 * time.Millisecond):
		t.Fatal("read-only plans on the same fixture were serialized")
	}
}

type fixtureRuntime struct {
	failCreateCall int
	failDeletePath string
	createCalls    int
	deletePaths    []string
}

func (r *fixtureRuntime) Invoke(_ context.Context, invocation api.Invocation) api.ProtocolResult {
	request := invocation.Payload.(api.HTTPRequestSpec)
	if request.Method == http.MethodDelete {
		r.deletePaths = append(r.deletePaths, request.Path)
		if request.Path == r.failDeletePath {
			return api.ProtocolResult{ErrorCategory: "injected", ErrorMessage: "delete failed"}
		}
	}
	if request.Method == http.MethodPost {
		r.createCalls++
		if r.createCalls == r.failCreateCall {
			return api.ProtocolResult{ErrorCategory: "injected", ErrorMessage: "create failed"}
		}
	}
	status := http.StatusOK
	if request.Method == http.MethodPost && (strings.Contains(request.Path, "configservice") ||
		strings.Contains(request.Path, "stationservice") || strings.Contains(request.Path, "priceservice") ||
		strings.Contains(request.Path, "travelservice")) {
		status = http.StatusCreated
	}
	return httpResult(status, 1, nil)
}

func TestPartialFixtureCreationCleansSuccessfulSteps(t *testing.T) {
	application := &Application{token: "token"}
	records := makeRecords("test", 7, 1)
	item := &records[0]
	runtime := &fixtureRuntime{failCreateCall: 2}

	if err := application.createRecord(context.Background(), runtime, item); err == nil {
		t.Fatal("injected fixture create failure unexpectedly passed")
	}
	if len(runtime.deletePaths) != 1 || !strings.Contains(runtime.deletePaths[0], "configservice") {
		t.Fatalf("partial fixture cleanup did not delete the created config: %v", runtime.deletePaths)
	}
	for index, created := range item.created {
		if created {
			t.Fatalf("fixture step %d remained marked created", index)
		}
	}
}

func TestFixtureCleanupContinuesAfterDeleteFailure(t *testing.T) {
	application := &Application{token: "token"}
	records := makeRecords("test", 7, 1)
	item := &records[0]
	for index := range item.created {
		item.created[index] = true
	}
	runtime := &fixtureRuntime{failDeletePath: "/api/v1/travelservice/trips/" + item.tripIn.TripID}

	if err := application.deleteRecord(context.Background(), runtime, item); err == nil {
		t.Fatal("injected fixture delete failure unexpectedly passed")
	}
	if len(runtime.deletePaths) != len(item.created) {
		t.Fatalf("cleanup attempted %d deletes, want %d", len(runtime.deletePaths), len(item.created))
	}
	if !item.created[6] {
		t.Fatal("failed delete was incorrectly marked clean")
	}
	for index := 0; index < 6; index++ {
		if item.created[index] {
			t.Fatalf("successful delete step %d remained marked created", index)
		}
	}
}

func TestEphemeralPlanIncludesNegativeReadAfterDelete(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	plan, err := application.BuildOperation(
		api.Operation{Name: "create_read_delete_config", Target: "config"},
		api.Sample{Counter: 1, Random: 2},
		data,
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Invocations) != 5 {
		t.Fatalf("ephemeral operation has %d invocations, want 5", len(plan.Invocations))
	}
	state := plan.State.(*operationState)
	if got := state.expectations[3].appStatus; got != 0 {
		t.Fatalf("negative read expects application status %d, want 0", got)
	}
}

func TestListPlanRejectsSchemaValidListThatOmitsSelectedRuntimeRecord(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "list_config", Target: "config"}
	plan, err := application.BuildOperation(operation, api.Sample{Random: 0}, data)
	if err != nil {
		t.Fatal(err)
	}
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusOK, 1, []configEntity{data.records[1].config}),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("list without selected record unexpectedly passed: %+v", validation)
	}
}

func TestListPlanRejectsDuplicateEntityKeys(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "list_train", Target: "train"}
	plan, err := application.BuildOperation(operation, api.Sample{Random: 0}, data)
	if err != nil {
		t.Fatal(err)
	}
	selected := data.records[0].train
	stale := selected
	stale.AverageSpeed++
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusOK, 1, []trainEntity{selected, stale}),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("duplicate list key unexpectedly passed: %+v", validation)
	}
}

func TestListPlanRejectsInvalidValuesInUnselectedRecords(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "list_station", Target: "station"}
	plan, err := application.BuildOperation(operation, api.Sample{Random: 0}, data)
	if err != nil {
		t.Fatal(err)
	}
	corrupt := map[string]any{
		"id": data.records[1].stationA.ID, "name": data.records[1].stationA.Name, "stayTime": "not-an-integer",
	}
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusOK, 1, []any{data.records[0].stationA, corrupt}),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "response_schema" {
		t.Fatalf("invalid unselected list value unexpectedly passed: %+v", validation)
	}
}

func TestUpdatePlansChangeAndProbeMutableSecondaryKeys(t *testing.T) {
	for _, service := range []string{"station", "route", "price"} {
		t.Run(service, func(t *testing.T) {
			application := &Application{token: "token"}
			data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
			operation := api.Operation{Name: "update_read_" + service, Target: service}
			plan, err := application.BuildOperation(operation, api.Sample{Counter: 3, Random: 0}, data)
			if err != nil {
				t.Fatal(err)
			}
			defer application.FinishOperation(plan)
			if len(plan.Invocations) != 3 {
				t.Fatalf("%s update has %d invocations, want update/read/retired-index probe", service, len(plan.Invocations))
			}
			retiredProbe := plan.Invocations[2].Payload.(api.HTTPRequestSpec).Path
			if service == "route" {
				expected := plan.State.(*operationState).expectations[1].expected.(routeEntity)
				if strings.Contains(retiredProbe, expected.StartStationID+"/"+expected.TerminalStationID) {
					t.Fatalf("route retired probe uses the new secondary key: %s", retiredProbe)
				}
			}
			if service == "price" {
				expected := plan.State.(*operationState).expectations[1].expected.(priceEntity)
				if strings.Contains(retiredProbe, expected.RouteID+"/"+expected.TrainType) {
					t.Fatalf("price retired probe uses the new secondary key: %s", retiredProbe)
				}
			}
		})
	}
}

func TestEphemeralPlanRejectsNoOpDelete(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "create_read_delete_config", Target: "config"}
	plan, err := application.BuildOperation(operation, api.Sample{Counter: 1, Random: 2}, data)
	if err != nil {
		t.Fatal(err)
	}
	created := plan.State.(*operationState).expectations[1].expected
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusCreated, 1, nil),
		httpResult(http.StatusOK, 1, created),
		httpResult(http.StatusOK, 1, nil),
		httpResult(http.StatusOK, 1, created),
		httpResult(http.StatusOK, 1, []configEntity{}),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "read_your_write" {
		t.Fatalf("no-op delete unexpectedly passed: %+v", validation)
	}
}

func TestEphemeralPlanRejectsDeletedRecordRetainedOnlyInList(t *testing.T) {
	application := &Application{token: "token"}
	data := &dataset{namespace: "test", records: makeRecords("test", 7, 2)}
	operation := api.Operation{Name: "create_read_delete_config", Target: "config"}
	plan, err := application.BuildOperation(operation, api.Sample{Counter: 1, Random: 2}, data)
	if err != nil {
		t.Fatal(err)
	}
	created := plan.State.(*operationState).expectations[1].expected.(configEntity)
	stale := created
	stale.Value = "stale-version"
	validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{
		httpResult(http.StatusCreated, 1, nil),
		httpResult(http.StatusOK, 1, created),
		httpResult(http.StatusOK, 1, nil),
		httpResult(http.StatusOK, 0, nil),
		httpResult(http.StatusOK, 1, []configEntity{stale}),
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("list-only delete leak unexpectedly passed: %+v", validation)
	}
}

func httpResult(status, appStatus int, data any) api.ProtocolResult {
	body, _ := json.Marshal(map[string]any{"status": appStatus, "msg": "ok", "data": data})
	return api.ProtocolResult{
		TransportSuccess: true,
		NativeStatus:     "test",
		Payload:          api.HTTPResponse{StatusCode: status, Body: body},
	}
}

func trainTicketWorkload() api.Workload {
	targets := make([]api.Target, 0, len(services))
	for _, service := range services {
		targets = append(targets, api.Target{Name: service, Protocol: "http"})
	}
	return api.Workload{
		Targets:    targets,
		Operations: []api.Operation{{Name: "read_config", Target: "config"}},
	}
}
