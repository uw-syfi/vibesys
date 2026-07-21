package hotel

import (
	"reflect"
	"testing"

	"vibesys/microservice-evaluator/api"
)

func TestSeedInputs(t *testing.T) {
	username, password, err := User(12)
	if err != nil {
		t.Fatal(err)
	}
	if username != "Cornell_3132" || password != "12121212121212121212" {
		t.Fatalf("seed user = (%q, %q)", username, password)
	}
	capacities := map[int]int{1: 200, 7: 300, 8: 250, 9: 200, 80: 250}
	for id, want := range capacities {
		got, err := Capacity(id)
		if err != nil || got != want {
			t.Fatalf("Capacity(%d) = %d, %v; want %d", id, got, err, want)
		}
	}
}

func TestValidateTopology(t *testing.T) {
	workload := api.Workload{
		Load:    api.Load{TimeoutSeconds: 2},
		Targets: []api.Target{{Name: GatewayTarget, Protocol: "http", SessionPolicy: "reuse"}},
	}
	if _, err := ValidateTopology(workload); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name   string
		mutate func(*api.Workload)
	}{
		{"missing", func(w *api.Workload) { w.Targets = nil }},
		{"protocol", func(w *api.Workload) { w.Targets[0].Protocol = "grpc" }},
		{"session", func(w *api.Workload) { w.Targets[0].SessionPolicy = "new_per_request" }},
		{"unknown config", func(w *api.Workload) { w.ApplicationConfig = map[string]any{"x": int64(1)} }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			candidate := workload
			candidate.Targets = append([]api.Target(nil), workload.Targets...)
			test.mutate(&candidate)
			if _, err := ValidateTopology(candidate); err == nil {
				t.Fatal("malformed topology was accepted")
			}
		})
	}
}

func TestPreflightIsDeterministic(t *testing.T) {
	left := PreflightProbes()
	right := PreflightProbes()
	for index := range left {
		if left[index].Name != right[index].Name || !reflect.DeepEqual(left[index].Invocation, right[index].Invocation) {
			t.Fatalf("probe %d differs", index)
		}
	}
}
