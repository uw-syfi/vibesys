// Command generator is the kv-store example in Go: bench.py's workload, operation for operation
// and draw for draw, so for the same phase it writes the same request stream as the Python
// generator (python/tests/e2e checks it), and the whole task as a Go benchmark
// (go/microservices).
//
// As the generator, the task image builds it next to bench.py (go build -o /task/kv-generator .)
// and bench_go.py names it: the phase pod runs `kv-generator generate ...` under loadgen. As a
// whole benchmark it is the task script itself:
//
//	kv-generator benchmark --vs-output records.jsonl [--seed N]
//
// against the application at $KV_TARGET (microservices.Local: started by whoever runs it).
// Whether the responses are right is the accuracy check's to judge:
//
//	kv-generator accuracy [--out DIR] [--seed N]
//
// runs two phases at a modest rate with the history captured and a pause between them, then
// judges the history with the oracle's specs (../oracle/kvcheck) in-process. The oracle program
// (../oracle) judges the history a benchmark with KV_CAPTURE=1 records the same way.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"time"

	"example.com/kv-store/oracle/kvcheck"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/microservices"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// Scale is what bench.py's constants and mix say.
type Scale struct {
	Seed                                  uint64
	Keys, Users                           int
	MinValue, MaxValue                    int
	ReadWeight, WriteWeight, DeleteWeight float64
}

// Example is bench.py's scale. Test is the end-to-end test's task (python/tests/vse_support/kv.py),
// chosen with KV_STORE_SCALE=test.
var (
	Example = Scale{Seed: 7, Keys: 100_000, Users: 100, MinValue: 16, MaxValue: 512, ReadWeight: 88, WriteWeight: 10, DeleteWeight: 2}
	Test    = Scale{Seed: 11, Keys: 64, Users: 4, MinValue: 4, MaxValue: 32, ReadWeight: 75, WriteWeight: 20, DeleteWeight: 5}
)

// Data is bench.py's Data as its dataset file holds it: the accounts as [user, password] pairs
// and the seeded values (bytes, base64 in the file).
type Data struct {
	Users  [][2]string `json:"users"`
	Values [][]byte    `json:"values"`
}

// Data is bench.py's Data(): the accounts and the values of half the keys.
func (s Scale) Data() (*Data, error) {
	data := &Data{}
	for n := range s.Users {
		data.Users = append(data.Users, [2]string{fmt.Sprintf("u%d", n), fmt.Sprintf("pw-%x", workload.NewRand(s.Seed, "user", n).Below(1<<40))})
	}
	for k := range s.Keys / 2 {
		data.Values = append(data.Values, s.value(workload.NewRand(s.Seed, "key", k)))
	}
	return data, nil
}

func readData(path string) (*Data, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	data := &Data{}
	if err := json.Unmarshal(raw, data); err != nil {
		return nil, err
	}
	if len(data.Users) == 0 {
		return nil, errors.New("the dataset has no users")
	}
	return data, nil
}

// Model is the dataset the operations draw accounts from.
type Model struct {
	data *Data
}

// value is bench.py's value(rand): a length, then that many lowercase letters.
func (s Scale) value(rand *workload.Rand) []byte {
	out := make([]byte, s.MinValue+rand.Below(s.MaxValue-s.MinValue+1))
	for i := range out {
		out[i] = byte(97 + rand.Below(26))
	}
	return out
}

var auth = workload.Concat("Bearer ", workload.Ref("token")) // filled from the login step's response

// login is the first step of every operation: it captures the token the second one sends.
func login(m *Model, rand *workload.Rand) workload.Step {
	user := m.data.Users[rand.Below(len(m.data.Users))]
	return workload.Post("/login",
		workload.JSON(workload.Object{{Key: "user", Value: user[0]}, {Key: "password", Value: user[1]}}),
		workload.Capture("token", "/token"))
}

type info struct {
	Key  int    `json:"key"`
	Seed uint64 `json:"seed,omitempty"`
}

// Workload is bench.py's WORKLOAD at a scale.
func (s Scale) Workload() workload.Workload[*Data, *Model] {
	read := func(m *Model, rand *workload.Rand) workload.Request {
		key := rand.Zipf(s.Keys, 0.99)
		return workload.Request{
			Steps: []workload.Step{login(m, rand),
				workload.Get(fmt.Sprintf("/kv/k%d", key), workload.Header("Authorization", auth), workload.Expect(200, 404))},
			Info: info{Key: key},
		}
	}
	write := func(m *Model, rand *workload.Rand) workload.Request {
		key, seed := rand.Zipf(s.Keys, 0.99), uint64(rand.Below(1<<62))
		return workload.Request{
			Steps: []workload.Step{login(m, rand),
				workload.Put(fmt.Sprintf("/kv/k%d", key), workload.Body(s.value(workload.NewRand(seed))), workload.Header("Authorization", auth))},
			Info: info{Key: key, Seed: seed},
		}
	}
	remove := func(m *Model, rand *workload.Rand) workload.Request {
		key := rand.Zipf(s.Keys, 0.99)
		return workload.Request{
			Steps: []workload.Step{login(m, rand),
				workload.HTTP("DELETE", fmt.Sprintf("/kv/k%d", key), workload.Header("Authorization", auth), workload.Expect(200, 404))},
			Info: info{Key: key},
		}
	}
	return workload.Workload[*Data, *Model]{
		Dataset: readData,
		Model:   func(data *Data, _ *workload.Rand) (*Model, error) { return &Model{data: data}, nil },
		Operations: []workload.Operation[*Model]{
			{Name: "read", Weight: s.ReadWeight, Build: read},
			{Name: "write", Weight: s.WriteWeight, Build: write},
			{Name: "delete", Weight: s.DeleteWeight, Build: remove},
		},
	}
}

// seed is bench.py's seed(data): the accounts and the keys, through the service's own API.
func seed(ctx context.Context, target string, data *Data) error {
	send := func(method, path, token string, body []byte) (*http.Response, error) {
		req, err := http.NewRequestWithContext(ctx, method, target+path, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := http.DefaultClient.Do(req)
		if err == nil && resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("%s %s: HTTP %d", method, path, resp.StatusCode)
		}
		return resp, err
	}
	for _, user := range data.Users {
		account, _ := json.Marshal(map[string]string{"user": user[0], "password": user[1]})
		resp, err := send("POST", "/register", "", account)
		if err != nil {
			return err
		}
		resp.Body.Close()
	}
	account, _ := json.Marshal(map[string]string{"user": data.Users[0][0], "password": data.Users[0][1]})
	resp, err := send("POST", "/login", "", account)
	if err != nil {
		return err
	}
	var login struct{ Token string }
	err = json.NewDecoder(resp.Body).Decode(&login)
	resp.Body.Close()
	if err != nil {
		return err
	}
	for key, value := range data.Values {
		resp, err := send("PUT", fmt.Sprintf("/kv/k%d", key), login.Token, value)
		if err != nil {
			return err
		}
		resp.Body.Close()
	}
	return nil
}

func main() {
	scale, values := Example, kvcheck.Example
	if os.Getenv("KV_STORE_SCALE") == "test" {
		scale, values = Test, kvcheck.Test
	}
	target := os.Getenv("KV_TARGET")
	capture := os.Getenv("KV_CAPTURE") != ""
	kv := scale.Workload()
	microservices.Main(microservices.Task[*Data, *Model]{
		Metrics: map[string]evaluator.Metric{
			"latency_ms.p50":        {Unit: "ms", Direction: evaluator.Min},
			"latency_ms.p99":        {Unit: "ms", Direction: evaluator.Min},
			"operations_per_second": {Unit: "ops/s", Direction: evaluator.Max},
		},
		Validity: &evaluator.Validity{MaxErrorRate: 0.001, MinSustainedRatio: 0.99, MinOpsPerOperation: 1},
		Dataset:  scale.Data,
		System:   microservices.Local(target),
		Seed:     func(ctx context.Context, data *Data) error { return seed(ctx, target, data) },
		Workload: &kv,
		Capture:  capture,
		// The accuracy check: on Kubernetes (microservices.K8s) its faults would be Restart,
		// RestartAll or Exec on the kv Deployment; a local application is the task's to pause.
		Accuracy: &microservices.Accuracy{
			Phases: 2,
			Load:   microservices.Open{Rate: 40, Secs: time.Second, Warmup: 500 * time.Millisecond, Arrivals: microservices.Uniform},
			Faults: []microservices.Fault{microservices.FaultFunc(0, "pause", "kv", func(context.Context) error { return nil })},
			Oracle: kvcheck.Oracle(values),
		},
		Benchmark: func(ctx context.Context, run *microservices.Run) error {
			rate, secs, warmup := 2000.0, 60*time.Second, 15*time.Second // bench.py's
			if scale == Test {
				rate, secs, warmup = 100, 2*time.Second, time.Second
			}
			if capture { // an accuracy run: a phase, a pause between, another phase
				if _, err := run.OpenLoop(ctx, microservices.Open{Rate: rate, Secs: secs, Warmup: warmup}); err != nil {
					return err
				}
				if err := run.Fault(ctx, "pause", "kv", func(context.Context) error { return nil }); err != nil {
					return err
				}
			}
			phase, err := run.OpenLoop(ctx, microservices.Open{Rate: rate, Secs: secs, Warmup: warmup})
			if err != nil {
				return err
			}
			return run.Report(map[string]float64{
				"latency_ms.p50": phase.P50Ms, "latency_ms.p99": phase.P99Ms, "operations_per_second": phase.OpsPerSec,
			})
		},
	})
}
