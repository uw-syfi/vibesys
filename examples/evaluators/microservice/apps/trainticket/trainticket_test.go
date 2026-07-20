package trainticket

import (
	"encoding/json"
	"net/http"
	"reflect"
	"testing"

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
	if len(plan.Invocations) != 4 {
		t.Fatalf("ephemeral operation has %d invocations, want 4", len(plan.Invocations))
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
	})
	application.FinishOperation(plan)
	if validation.Success || validation.ErrorCategory != "read_your_write" {
		t.Fatalf("no-op delete unexpectedly passed: %+v", validation)
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
