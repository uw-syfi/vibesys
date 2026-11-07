package wire_test

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const costCorpus = "../../../proto/vibesys/orchestrator/v1/conformance/cost/"

// costFrames are the cost frames the corpus holds, each worked out by the ledger from a run that is
// written out here, so that the corpus is what the host sends and not a hand-written guess.
func costFrames() map[string]*pb.WatchRunCostResponse {
	begin := time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)
	lead := core.Agent{ID: "lead", Role: core.RoleLead, Spec: core.AgentSpec{Harness: core.ClaudeCode}}
	worker := core.Agent{ID: "w1", Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.Codex, Model: "model-x"}}
	profiler := core.Agent{ID: "p1", Role: core.RoleProfiler, Spec: core.AgentSpec{Harness: core.ClaudeCode, Model: "model-small"}}
	turn := func(a core.Agent, ordinal uint32, at time.Duration, u core.Usage) core.Turn {
		return core.Turn{ID: core.TurnID(string(a.ID) + string(rune('0'+ordinal))), Agent: a.ID, Ordinal: ordinal, Status: core.TurnCompleted, Usage: u, EndedAt: begin.Add(at)}
	}
	frame := func(l *cost.Ledger, limit float64, now time.Duration) *pb.WatchRunCostResponse {
		v := l.View(core.Run{ID: "run-1", CreatedAt: begin, MaxCostUSD: limit}, begin.Add(now))
		return &pb.WatchRunCostResponse{Cost: wire.CostMessage(v, 41)}
	}

	var live cost.Ledger
	for _, a := range []core.Agent{lead, worker, profiler} {
		live.Agent(a)
	}
	live.Add(turn(lead, 1, 5*time.Minute, core.Usage{InputTokens: tokensOf(1200), OutputTokens: tokensOf(300), CostUSD: dollarsOf(0.75)}), lead)
	live.Add(turn(worker, 1, 70*time.Minute, core.Usage{InputTokens: tokensOf(9000), CacheReadTokens: tokensOf(40000), ReasoningTokens: tokensOf(500), CostUSD: dollarsOf(2.5)}), worker)
	live.Add(turn(worker, 2, 150*time.Minute, core.Usage{InputTokens: tokensOf(8000), CostUSD: dollarsOf(1.25)}), worker)
	live.Add(turn(profiler, 1, 200*time.Minute, core.Usage{InputTokens: tokensOf(700), OutputTokens: tokensOf(90)}), profiler)
	live.Add(turn(lead, 2, 290*time.Minute, core.Usage{OutputTokens: tokensOf(450), CostUSD: dollarsOf(0.5)}), lead)

	var unpriced cost.Ledger
	unpriced.Agent(worker)
	unpriced.Add(turn(worker, 1, 3*time.Minute, core.Usage{InputTokens: tokensOf(5000), OutputTokens: tokensOf(40)}), worker)

	return map[string]*pb.WatchRunCostResponse{
		"live.json":     frame(&live, 500, 300*time.Minute),
		"unpriced.json": frame(&unpriced, 0, 10*time.Minute),
	}
}

// The corpus is what the ledger gives. A change to the view that is meant shows up here as a
// diff of these files; `VIBESYS_UPDATE_GOLDEN=1 go test ./internal/wire -run CostCorpus` writes them.
func TestCostCorpusIsWhatTheLedgerGives(t *testing.T) {
	frames := costFrames()
	files, err := filepath.Glob(costCorpus + "*.json")
	if err != nil || len(files) != len(frames) {
		if os.Getenv("VIBESYS_UPDATE_GOLDEN") == "" {
			t.Fatalf("corpus holds %d files, want %d: %v %v", len(files), len(frames), files, err)
		}
	}
	for name, want := range frames {
		t.Run(name, func(t *testing.T) {
			if err := wire.Validate(want); err != nil {
				t.Fatalf("the contract refuses what the ledger gave: %v", err)
			}
			path := costCorpus + name
			if os.Getenv("VIBESYS_UPDATE_GOLDEN") != "" {
				write(t, path, want)
			}
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			got := new(pb.WatchRunCostResponse)
			if err := protojson.Unmarshal(data, got); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(got, want) {
				t.Fatalf("%s is not what the ledger gives; rerun with VIBESYS_UPDATE_GOLDEN=1 and review the diff:\n got %v\nwant %v", name, got, want)
			}
		})
	}
}

// write stores a frame as indented JSON. protojson varies its spacing from build to build, so the
// text is compacted and indented again to keep a rewrite from showing a diff that is not one.
func write(t *testing.T, path string, m proto.Message) {
	t.Helper()
	raw, err := protojson.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var compact, pretty bytes.Buffer
	if err := json.Compact(&compact, raw); err != nil {
		t.Fatal(err)
	}
	if err := json.Indent(&pretty, compact.Bytes(), "", "  "); err != nil {
		t.Fatal(err)
	}
	pretty.WriteByte('\n')
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, pretty.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}
