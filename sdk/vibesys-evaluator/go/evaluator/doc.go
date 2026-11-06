// Package evaluator is the domain-neutral core of vibesys-evaluator: the sample lifecycle every
// task evaluator shares, the metrics and validity rules a benchmark declares, the
// result-protocol-2 records vibesys-cli reads, HDR histograms and how trials combine into a
// metric. It knows no domain; the domains (microservices, llm_serving) build on it.
//
// A Go task script is a whole benchmark:
//
//	func main() {
//		evaluator.Main(evaluator.Task{
//			Metrics: map[string]evaluator.Metric{
//				"operations_per_second": {Unit: "ops/s", Direction: evaluator.Max},
//			},
//			Benchmark: func(ctx context.Context, run *evaluator.Run) error {
//				// measure ...
//				return run.Report(map[string]float64{"operations_per_second": 5300})
//			},
//		})
//	}
//
// The command line is that of the Python main: `check`, `benchmark --vs-output FILE [--seed N]`
// and `profile [--seed N]`; see docs/spec.md ("The sample"). The Python package calls the same
// code through the `vibesys-evaluator` binary (cmd/vibesys-evaluator).
package evaluator
