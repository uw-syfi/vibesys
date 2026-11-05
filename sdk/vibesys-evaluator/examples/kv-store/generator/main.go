// Command generator is the kv-store example's request generator in Go: the same workload as
// bench.py's WORKLOAD, operation for operation and draw for draw, so for the same phase it writes
// the same request stream as the Python generator (python/tests/e2e checks it). bench_go.py runs
// the task with it.
//
// Build it into the task image next to bench.py (go build -o /task/kv-generator .); the phase
// pod runs it as `kv-generator generate ...` under loadgen and `kv-generator check ...` after.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/workload"
)

// Scale is what bench.py's constants and mix say; the dataset itself is read from the file
// the orchestrator writes from bench.py's Data.
type Scale struct {
	Keys                    int
	MinValue, MaxValue      int
	ReadWeight, WriteWeight float64
	ReadSample              float64
}

// Example is bench.py's scale. Test is the end-to-end test's task (python/tests/vse_support/kv.py),
// chosen with KV_STORE_SCALE=test.
var (
	Example = Scale{Keys: 100_000, MinValue: 16, MaxValue: 512, ReadWeight: 90, WriteWeight: 10, ReadSample: 0.25}
	Test    = Scale{Keys: 64, MinValue: 4, MaxValue: 32, ReadWeight: 80, WriteWeight: 20, ReadSample: 0.5}
)

// Data is bench.py's Data as its dataset file holds it: the accounts as [user, password] pairs
// and the seeded values (bytes, base64 in the file).
type Data struct {
	Users  [][2]string `json:"users"`
	Values [][]byte    `json:"values"`
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

// stored is a key's value as a read may see it: absent (404) or a body.
type stored struct {
	present bool
	body    string
}

// Model is the dataset and the values written, with the visibility rule of bound 0 (a read
// sees every acknowledged write at once).
type Model struct {
	data   *Data
	values *workload.Versions[int, stored]
}

func (m *Model) seeded(key int) stored {
	if key < len(m.data.Values) {
		return stored{true, string(m.data.Values[key])}
	}
	return stored{}
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
			Info:      info{Key: key, Seed: seed},
			Exclusive: fmt.Sprintf("k%d", key),
		}
	}
	applyWrite := func(m *Model, call workload.Call) error {
		var i info
		if err := call.DecodeInfo(&i); err != nil {
			return err
		}
		m.values.Write(i.Key, stored{true, string(s.value(workload.NewRand(i.Seed)))}, call)
		return nil
	}
	checkRead := func(m *Model, call workload.Call) error {
		var i info
		if err := call.DecodeInfo(&i); err != nil {
			return err
		}
		response := call.Responses[1]
		got := stored{}
		if response.Status == 200 {
			got = stored{true, string(response.Body)}
		}
		allowed := m.values.Allowed(i.Key, m.seeded(i.Key), call.Start, call.End)
		for _, v := range allowed {
			if v == got {
				return nil
			}
		}
		return fmt.Errorf("k%d: %.40q, allowed %d values", i.Key, got.body, len(allowed))
	}
	checkWrite := func(_ *Model, call workload.Call) error {
		if status := call.Responses[1].Status; status != 200 {
			return fmt.Errorf("PUT: HTTP %d", status)
		}
		return nil
	}
	return workload.Workload[*Data, *Model]{
		Dataset: readData,
		Model: func(data *Data, _ *workload.Rand) (*Model, error) {
			return &Model{data: data, values: workload.NewVersions[int, stored](time.Duration(0))}, nil
		},
		Operations: []workload.Operation[*Model]{
			// A share of the reads is checked: the bodies of the others are kept as hash and length.
			{Name: "read", Weight: s.ReadWeight, Build: read, Check: checkRead, Sample: s.ReadSample},
			{Name: "write", Weight: s.WriteWeight, Build: write, Check: checkWrite, Apply: applyWrite},
		},
	}
}

func main() {
	scale := Example
	if os.Getenv("KV_STORE_SCALE") == "test" {
		scale = Test
	}
	workload.Main(scale.Workload())
}
