package registry

import (
	"context"
	"strings"
	"testing"

	"vibesys/microservice-evaluator/api"
)

type namedApplication struct{ name string }

func (a *namedApplication) Name() string { return a.name }
func (*namedApplication) Prepare(context.Context, api.Runtime, api.TrialContext) (any, error) {
	return nil, nil
}
func (*namedApplication) Reset(context.Context, api.Runtime, api.TrialContext) error { return nil }
func (*namedApplication) BuildOperation(api.Operation, api.Sample, any) (api.OperationPlan, error) {
	return api.OperationPlan{}, nil
}
func (*namedApplication) ValidateOperation(api.Operation, api.OperationPlan, []api.ProtocolResult) api.ValidationResult {
	return api.ValidationResult{}
}
func (*namedApplication) FinishOperation(api.OperationPlan) {}

type namedAccuracyApplication struct{ name string }

func (a *namedAccuracyApplication) Name() string                        { return a.name }
func (*namedAccuracyApplication) Properties() []api.AccuracyProperty    { return nil }
func (*namedAccuracyApplication) ReadinessProbes() []api.ReadinessProbe { return nil }
func (*namedAccuracyApplication) Check(context.Context, api.Runtime, api.AccuracyContext, api.AccuracyRecorder) error {
	return nil
}

func TestRegistryRejectsNilAndMismatchedApplicationFactories(t *testing.T) {
	for _, test := range []struct {
		name     string
		accuracy bool
		nilValue bool
	}{
		{"benchmark nil", false, true},
		{"benchmark mismatch", false, false},
		{"accuracy nil", true, true},
		{"accuracy mismatch", true, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			registered := New()
			workload := api.Workload{Application: "wanted"}
			var err error
			if test.accuracy {
				err = registered.RegisterAccuracyApplication("wanted", func(api.Workload) (api.AccuracyApplication, error) {
					if test.nilValue {
						var application *namedAccuracyApplication
						return application, nil
					}
					return &namedAccuracyApplication{name: "wrong"}, nil
				})
				if err == nil {
					_, err = registered.AccuracyApplication(workload)
				}
			} else {
				err = registered.RegisterApplication("wanted", func(api.Workload) (api.Application, error) {
					if test.nilValue {
						var application *namedApplication
						return application, nil
					}
					return &namedApplication{name: "wrong"}, nil
				})
				if err == nil {
					_, err = registered.Application(workload)
				}
			}
			if err == nil || (!strings.Contains(err.Error(), "nil") && !strings.Contains(err.Error(), "wrong")) {
				t.Fatalf("factory error=%v", err)
			}
		})
	}
}
