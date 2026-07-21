package trainticket_test

import (
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
