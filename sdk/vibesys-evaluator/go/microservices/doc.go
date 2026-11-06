// Package microservices is vibesys-evaluator's phase runner for applications served over HTTP:
// load phases (Run.ClosedLoop, Run.OpenLoop) against a deployed application, each in a phase pod
// next to it where loadgen sends the request stream of the task's generator (package workload),
// then the saturation guard and the task's validity rules decide whether the phase is a
// measurement. It is the Go form of Python's vibesys_evaluator.microservices, whose Run calls the
// same code through the `vibesys-evaluator phase` subcommands.
//
// A Go task script is a whole benchmark:
//
//	func main() {
//		microservices.Main(microservices.Task[Data, *Model]{
//			Metrics:  map[string]evaluator.Metric{"ops_per_sec": {Unit: "ops/s", Direction: evaluator.Max}},
//			Dataset:  dataset,
//			System:   microservices.Local("http://127.0.0.1:8080"),
//			Workload: &kvWorkload,
//			Benchmark: func(ctx context.Context, run *microservices.Run) error {
//				capacity, err := run.ClosedLoop(ctx, microservices.Closed{Clients: 64, Secs: 30 * time.Second, Warmup: 10 * time.Second})
//				if err != nil {
//					return err
//				}
//				return run.Report(map[string]float64{"ops_per_sec": capacity.OpsPerSec})
//			},
//		})
//	}
//
// The application runs on a System: Local (already running), or K8s (built and deployed into a
// Kubernetes namespace of its own per sample).
//
// A benchmark only measures: whether the responses are right is for an accuracy check (package
// check) to judge, from the history a run with Task.Capture records (every response body, the
// loadgen client of each operation, the generator's info, the faults of Run.Fault). Task.Accuracy
// is that check as a command of the same binary (`accuracy`): phases of a fixed open-loop load
// with capture on, fault actions between them (Restart, RestartAll, Exec, FaultFunc), and the
// oracle's specs judging the history. See sdk/vibesys-evaluator/docs/spec.md, "A phase",
// "Accuracy history" and "Accuracy run".
package microservices
