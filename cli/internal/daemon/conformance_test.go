package daemon_test

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The scenarios of proto/vibesys/cli/v1/conformance/task_watch are read here against the real
// server, and by the orchestrator's fake daemon against the same files. See the README beside them.

var updateConformance = flag.Bool("update-conformance", false, "write the messages a scenario got into its expect")

const conformanceDir = "../../../proto/vibesys/cli/v1/conformance/task_watch"

type scenario struct {
	DuringReset   []scenarioOp       `json:"during_reset,omitempty"`
	AfterMessages int                `json:"after_messages,omitempty"`
	Lifecycle     bool               `json:"lifecycle,omitempty"`
	Description   string             `json:"description"`
	JudgedCommits uint32             `json:"judged_commits"`
	Setup         []scenarioOp       `json:"setup"`
	Ops           []scenarioOp       `json:"ops"`
	Cuts          []int              `json:"cuts"`
	Expect        [][]map[string]any `json:"expect"`
}

type scenarioOp struct {
	Op      scenarioOperation `json:"op"`
	Job     string            `json:"job,omitempty"`
	Run     string            `json:"run,omitempty"`
	Agent   string            `json:"agent,omitempty"`
	Commit  string            `json:"commit,omitempty"`
	Samples string            `json:"samples,omitempty"`
	Title   string            `json:"title,omitempty"`
	Body    string            `json:"body,omitempty"`
	Number  uint32            `json:"number,omitempty"`
	Text    string            `json:"text,omitempty"`
}

var sampleSets = map[string][]float64{"level": level, "win": win, "lose": lose}
var (
	level = []float64{100, 101, 99, 100, 102}
	win   = []float64{120, 121, 119, 122, 120}
	lose  = []float64{80, 81, 79, 80, 82}
)

// commitOf is the commit a scenario calls c0, c1 or c2.
func commitOf(t *testing.T, name string) core.Commit {
	t.Helper()
	var n int
	if _, err := fmt.Sscanf(name, "c%d", &n); err != nil || n < 0 || n > 2 {
		t.Fatalf("commit %q: the scenarios have c0, c1 and c2", name)
	}
	return cm(n)
}

var timestamp = regexp.MustCompile(`"\d{4}-\d\d-\d\dT[0-9:.]+Z"`)

// normalized is a message as JSON with what differs by boot or by clock replaced: the epoch, the
// times and the commits.
func normalized(m *cliv1.WatchTaskResponse) map[string]any {
	raw, _ := protojson.MarshalOptions{UseProtoNames: false}.Marshal(m)
	text := timestamp.ReplaceAllString(string(raw), `"T"`)
	for n := 0; n <= 2; n++ {
		text = strings.ReplaceAll(text, string(cm(n)), fmt.Sprintf("c%d", n))
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(text), &out); err != nil {
		panic(err)
	}
	out["epoch"] = "E1"
	if _, has := out["sequence"]; !has {
		out["sequence"] = "0"
	}
	return out
}

// view is what a client of the stream holds, applied as the contract says: a reset is held aside
// until CaughtUp.
type view struct {
	held, pending map[string]string
	resetting     bool
}

func (v *view) apply(m *cliv1.WatchTaskResponse) {
	switch b := m.GetBody().(type) {
	case *cliv1.WatchTaskResponse_Reset_:
		v.resetting, v.pending = true, map[string]string{}
	case *cliv1.WatchTaskResponse_Facts:
		into := v.held
		if v.resetting {
			into = v.pending
		}
		if into == nil {
			into = map[string]string{}
			v.held = into
		}
		for _, f := range b.Facts.GetFacts() {
			into[conformanceKey(f)] = fmt.Sprint(f)
		}
	case *cliv1.WatchTaskResponse_CaughtUp:
		v.held, v.pending, v.resetting = v.pending, nil, false
	}
}

func conformanceKey(f *cliv1.TaskFact) string {
	switch v := f.GetFact().(type) {
	case *cliv1.TaskFact_Job:
		return "job/" + v.Job.GetId()
	case *cliv1.TaskFact_Result:
		return "result/" + v.Result.GetJobId()
	case *cliv1.TaskFact_Judgment:
		return "judgment/" + v.Judgment.GetCommit()
	case *cliv1.TaskFact_Promotion:
		return fmt.Sprint("promotion/", v.Promotion.GetSeq())
	case *cliv1.TaskFact_Plan:
		return fmt.Sprint("plan/", v.Plan.GetNumber())
	case *cliv1.TaskFact_Head:
		return "head"
	case *cliv1.TaskFact_Health:
		return "health"
	case *cliv1.TaskFact_Trouble:
		return "trouble/" + v.Trouble.GetJobId()
	}
	return "?"
}

// holds says whether the view is the task as a look at it finds it.
func (v *view) holds(look core.TaskView) bool {
	if v.held == nil || v.resetting {
		return false
	}
	want := map[string]string{}
	for _, f := range look.Facts() {
		m, _ := wire.TaskFact(f)
		want[conformanceKey(m)] = fmt.Sprint(m)
	}
	for k, val := range want {
		if v.held[k] != val {
			return false
		}
	}
	for k := range v.held {
		if _, ok := want[k]; !ok && !strings.HasPrefix(k, "judgment/") {
			return false
		}
	}
	return true
}

func TestTaskWatchConformance(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(conformanceDir, "*.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no scenarios in %s: %v", conformanceDir, err)
	}
	ran := 0
	for _, file := range files {
		t.Run(strings.TrimSuffix(filepath.Base(file), ".json"), func(t *testing.T) {
			raw, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			var sc scenario
			dec := json.NewDecoder(bytes.NewReader(raw))
			dec.DisallowUnknownFields()
			if err := dec.Decode(&sc); err != nil {
				t.Fatalf("%s: %v", file, err)
			}
			var got [][]map[string]any
			if sc.Lifecycle {
				got = runLifecycleScenario(t, sc)
			} else {
				got = runScenario(t, sc)
			}
			ran++
			if *updateConformance {
				sc.Expect = got
				out, err := marshalScenario(sc)
				if err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(file, out, 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			if !reflect.DeepEqual(got, sc.Expect) {
				want, _ := json.MarshalIndent(sc.Expect, "", " ")
				have, _ := json.MarshalIndent(got, "", " ")
				t.Fatalf("%s: the stream said\n%s\nwant\n%s\n(rerun with -update-conformance to write what the server says)", file, have, want)
			}
		})
	}
	if ran != len(files) {
		t.Fatalf("ran %d of %d scenarios", ran, len(files))
	}
}

func marshalScenario(sc scenario) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(sc); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// runScenario plays the scenario on a daemon over fakes and returns the messages of each stream.
func runScenario(t *testing.T, sc scenario) [][]map[string]any {
	t.Helper()
	r := newResultsRig(t)
	ctx := ctxFor(t)
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for _, op := range sc.Setup {
		switch op.Op {
		case "measure":
			at = at.Add(time.Minute)
			r.measuredAt(commitOf(t, op.Commit), sampleSets[op.Samples], at)
		default:
			t.Fatalf("setup op %q: setup has measure", op.Op)
		}
	}
	scope := mustScope(t, "/repo", "task")
	open := func() (cliv1.DaemonService_WatchTaskClient, context.CancelFunc) {
		sctx, cancel := context.WithCancel(ctx)
		s, err := r.client.WatchTask(sctx, &cliv1.WatchTaskRequest{Context: call, JudgedCommits: sc.JudgedCommits})
		if err != nil {
			t.Fatal(err)
		}
		return s, cancel
	}
	var streams [][]map[string]any
	var stream cliv1.DaemonService_WatchTaskClient
	var cancel context.CancelFunc
	var v view
	var current []map[string]any
	recv := func() {
		m, err := stream.Recv()
		if err != nil {
			t.Fatalf("Recv: %v", err)
		}
		v.apply(m)
		current = append(current, normalized(m))
	}
	settle := func() {
		for {
			look, ok := r.app.TaskView(ctx, scope, int(coreJudged(sc.JudgedCommits)))
			if ok && v.holds(look) {
				return
			}
			recv()
		}
	}
	// The cuts: stream k is cut after cuts[k] messages, and the next one begins again with a reset.
	for _, n := range sc.Cuts {
		stream, cancel = open()
		v, current = view{}, nil
		for len(current) < n {
			recv()
		}
		cancel()
		streams = append(streams, current)
	}
	stream, cancel = open()
	defer cancel()
	v, current = view{}, nil
	settle()
	for _, op := range sc.Ops {
		play(t, r, op)
		settle()
	}
	return append(streams, current)
}

func coreJudged(asked uint32) uint32 {
	n, _ := core.ResolveJudgedCommits(asked)
	return uint32(n)
}

func play(t *testing.T, r *resultsRig, op scenarioOp) {
	t.Helper()
	ctx := ctxFor(t)
	var err error
	switch op.Op {
	case "promote":
		_, err = r.client.Promote(ctx, &cliv1.PromoteRequest{Context: call, Commit: string(commitOf(t, op.Commit))})
	case "create_plan":
		_, err = r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: op.Title, Body: op.Body})
	case "comment_plan":
		_, err = r.client.CommentPlan(ctx, &cliv1.CommentPlanRequest{Context: call, Number: op.Number, Text: op.Text})
	default:
		t.Fatalf("op %q is not one of promote, create_plan, comment_plan", op.Op)
	}
	if err != nil {
		t.Fatalf("%s: %v", op.Op, err)
	}
}
