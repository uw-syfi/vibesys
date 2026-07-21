package trainticket_test

import (
	"reflect"
	"testing"

	accuracytrainticket "vibesys/microservice-evaluator/accuracyapps/trainticket"
	"vibesys/microservice-evaluator/api"
	benchmarktrainticket "vibesys/microservice-evaluator/apps/trainticket"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

func TestBenchmarkAndAccuracyRejectSameMalformedSharedConfig(t *testing.T) {
	workload := api.Workload{
		Load:              api.Load{TimeoutSeconds: 1},
		ApplicationConfig: map[string]any{"records": "garbage"},
	}
	for _, service := range trainticketsupport.Services() {
		workload.Targets = append(workload.Targets, api.Target{
			Name: service, Protocol: "http", SessionPolicy: "reuse",
		})
	}
	if _, err := benchmarktrainticket.New(workload); err == nil {
		t.Fatal("benchmark accepted malformed shared config")
	}
	if _, err := accuracytrainticket.New(workload); err == nil {
		t.Fatal("accuracy accepted malformed shared config")
	}
}

func TestBenchmarkAndAccuracyUseIdenticalPreflightPlans(t *testing.T) {
	workload := api.Workload{
		Load:              api.Load{TimeoutSeconds: 1, Seed: 73},
		ApplicationConfig: map[string]any{"records": int64(8)},
	}
	for _, service := range trainticketsupport.Services() {
		workload.Targets = append(workload.Targets, api.Target{
			Name: service, Protocol: "http", SessionPolicy: "reuse",
		})
	}
	benchmark, err := benchmarktrainticket.New(workload)
	if err != nil {
		t.Fatal(err)
	}
	accuracy, err := accuracytrainticket.New(workload)
	if err != nil {
		t.Fatal(err)
	}
	benchmarkPreflight := benchmark.(api.PreflightApplication)
	assertInvocationsEqual(
		t, benchmarkPreflight.ReadinessProbes(), accuracy.ReadinessProbes(),
	)
	assertModeNeutralInvocations(
		t, benchmarkPreflight.PreflightProbes(), accuracy.PreflightProbes(),
	)
}

func assertModeNeutralInvocations(t *testing.T, left, right []api.ReadinessProbe) {
	t.Helper()
	redact := func(probes []api.ReadinessProbe) []api.ReadinessProbe {
		cloned := append([]api.ReadinessProbe(nil), probes...)
		for index := range cloned {
			invocation := cloned[index].Invocation
			spec := invocation.Payload.(api.HTTPRequestSpec)
			spec.Headers = map[string]string{"Authorization": "Bearer <redacted>"}
			invocation.Payload = spec
			cloned[index].Invocation = invocation
		}
		return cloned
	}
	assertInvocationsEqual(t, redact(left), redact(right))
}

func assertInvocationsEqual(t *testing.T, left, right []api.ReadinessProbe) {
	t.Helper()
	if len(left) != len(right) {
		t.Fatalf("probe lengths differ: %d != %d", len(left), len(right))
	}
	for index := range left {
		if left[index].Name != right[index].Name ||
			!reflect.DeepEqual(left[index].Invocation, right[index].Invocation) {
			t.Fatalf("probe %d differs: %+v != %+v", index, left[index], right[index])
		}
	}
}
