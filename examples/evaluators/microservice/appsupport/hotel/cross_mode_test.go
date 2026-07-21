package hotel_test

import (
	"reflect"
	"testing"

	accuracyhotel "vibesys/microservice-evaluator/accuracyapps/hotel"
	"vibesys/microservice-evaluator/api"
	benchmarkhotel "vibesys/microservice-evaluator/apps/hotel"
	"vibesys/microservice-evaluator/appsupport/hotel"
)

func workload() api.Workload {
	return api.Workload{
		Load: api.Load{TimeoutSeconds: 1, Repetitions: 1, Seed: 73},
		Targets: []api.Target{{
			Name: hotel.GatewayTarget, Protocol: "http", SessionPolicy: "reuse",
		}},
		Operations: []api.Operation{{
			Name: "search_hotels", Target: hotel.GatewayTarget,
		}},
	}
}

func TestBenchmarkAndAccuracyRejectSameMalformedSharedConfig(t *testing.T) {
	candidate := workload()
	candidate.ApplicationConfig = map[string]any{"unknown": int64(1)}
	if _, err := benchmarkhotel.New(candidate); err == nil {
		t.Fatal("benchmark accepted malformed shared config")
	}
	if _, err := accuracyhotel.New(candidate); err == nil {
		t.Fatal("accuracy accepted malformed shared config")
	}
}

func TestBenchmarkAndAccuracyUseIdenticalPreflightPlans(t *testing.T) {
	benchmark, err := benchmarkhotel.New(workload())
	if err != nil {
		t.Fatal(err)
	}
	accuracy, err := accuracyhotel.New(workload())
	if err != nil {
		t.Fatal(err)
	}
	benchmarkPreflight := benchmark.(api.PreflightApplication)
	assertInvocationsEqual(t, benchmarkPreflight.ReadinessProbes(), accuracy.ReadinessProbes())
	assertInvocationsEqual(t, benchmarkPreflight.PreflightProbes(), accuracy.PreflightProbes())
}

func assertInvocationsEqual(t *testing.T, left, right []api.ReadinessProbe) {
	t.Helper()
	if len(left) != len(right) {
		t.Fatalf("probe lengths differ: %d != %d", len(left), len(right))
	}
	for index := range left {
		if left[index].Name != right[index].Name || !reflect.DeepEqual(left[index].Invocation, right[index].Invocation) {
			t.Fatalf("probe %d differs: %+v != %+v", index, left[index], right[index])
		}
	}
}
