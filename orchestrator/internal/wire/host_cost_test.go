package wire_test

import (
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

var hostBegin = time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)

// hostWith builds a host from runs that each have a lead and the given turns, as a stream tells them.
type hostRun struct {
	run   core.Run
	agent core.Agent
	turns []core.Turn
}

func buildHost(since time.Time, runs ...hostRun) *cost.Host {
	h := cost.NewHost(since)
	for _, r := range runs {
		h.Run(r.run)
		h.Agent(r.agent)
		for _, t := range r.turns {
			h.Add(t, r.agent)
		}
	}
	return h
}

func lineRun(id string, status core.RunStatus, limit float64, spec core.AgentSpec, turns ...core.Usage) hostRun {
	run := core.Run{ID: core.RunID(id), Task: "task " + id, Status: status, MaxCostUSD: limit, CreatedAt: hostBegin}
	if status.Final() {
		run.EndedAt = hostBegin.Add(4 * time.Hour)
	}
	agent := core.Agent{ID: core.AgentID(id + "-lead"), Run: run.ID, Role: core.RoleLead, Spec: spec}
	out := hostRun{run: run, agent: agent}
	for i, u := range turns {
		out.turns = append(out.turns, core.Turn{
			ID: core.TurnID(fmt.Sprintf("%s-t%d", id, i)), Agent: agent.ID, Ordinal: uint32(i + 1), Status: core.TurnCompleted,
			Usage: u, EndedAt: hostBegin.Add(time.Duration(i+1) * 30 * time.Minute),
		})
	}
	return out
}

// A host cost view reaches a client as the contract states it, whatever the host holds: no run, runs
// with and without a price or a limit, a run with no turn, more runs than the list holds, and run
// names and model names longer than the limits, whose cuts fall inside a character. Every message
// meets the contract's rules and survives the wire, and says in the order it is given what the view
// said.
func TestHostCostMessageMeetsTheContract(t *testing.T) {
	r := rand.New(rand.NewSource(11))
	// Names mix one-byte and three-byte characters, so a cut at the limit can fall inside one.
	long := func(prefix string, n int) string {
		var b strings.Builder
		b.WriteString(prefix)
		for b.Len() < n {
			b.WriteString([]string{"x", "世"}[r.Intn(2)])
		}
		return b.String()
	}
	for _, runs := range []int{0, 1, 2, cost.MaxHostRuns - 1, cost.MaxHostRuns, cost.MaxHostRuns + 1, 3 * cost.MaxHostRuns} {
		var all []hostRun
		for i := range runs {
			spec := core.AgentSpec{Harness: core.Codex, Model: core.ModelName(long(fmt.Sprintf("m%03d-", i), r.Intn(3*wire.MaxModelNameBytes)))}
			usage := []core.Usage{{InputTokens: tokensOf(uint64(1 + r.Intn(50))), CostUSD: dollarsOf(float64(r.Intn(40)) / 4)}}
			switch r.Intn(4) {
			case 0:
				usage = nil
			case 1:
				usage = []core.Usage{{OutputTokens: tokensOf(7)}}
			}
			limit := 0.0
			if r.Intn(2) == 0 {
				limit = float64(1 + r.Intn(100))
			}
			status := []core.RunStatus{core.RunRunning, core.RunEnded, core.RunFailed, core.RunStopping, core.RunStarting}[r.Intn(5)]
			run := lineRun(fmt.Sprintf("run-%03d", i), status, limit, spec, usage...)
			run.run.Task = long("t", r.Intn(4*wire.MaxRunNameBytes)+1)
			all = append(all, run)
		}
		v := buildHost(time.Time{}, all...).View(hostBegin.Add(5 * time.Hour))
		msg := wire.HostCostMessage(v, 42)
		if err := wire.Validate(msg); err != nil {
			t.Fatalf("%d runs: the contract refuses the message: %v", runs, err)
		}
		data, err := proto.Marshal(msg)
		if err != nil {
			t.Fatal(err)
		}
		var back pb.HostCost
		if err := proto.Unmarshal(data, &back); err != nil || !proto.Equal(msg, &back) {
			t.Fatalf("%d runs: lost on the wire (%v)", runs, err)
		}
		keep := min(runs, cost.MaxHostRuns)
		if len(msg.Runs) != keep || msg.RunCount != uint32(runs) || (msg.OtherRuns != nil) != (runs > keep) || msg.Sequence != 42 {
			t.Fatalf("%d runs: %d lines, run_count %d, others %v", runs, len(msg.Runs), msg.RunCount, msg.OtherRuns)
		}
		for i, line := range msg.Runs {
			want := v.Runs[i]
			if line.RunId != string(want.Run) || line.TotalTokens != want.Tokens || line.Turns != want.Turns || line.UnpricedTurns != want.Unpriced {
				t.Errorf("line %d is %v, want %+v", i, line, want)
			}
			if !utf8.ValidString(line.Name) || len(line.Name) > wire.MaxRunNameBytes || !strings.HasPrefix(want.Name, line.Name) || (len(want.Name) <= wire.MaxRunNameBytes) != (line.Name == want.Name) {
				t.Errorf("line %d name of %d bytes is not a cut of the task of %d bytes", i, len(line.Name), len(want.Name))
			}
			if wantStatus := wire.RunMessage(core.Run{Status: want.Status}).Status; line.Status != wantStatus {
				t.Errorf("line %d status %v, want %v", i, line.Status, wantStatus)
			}
			if (line.MaxCostUsd != nil) != (want.MaxCostUSD > 0) || (line.Usage.CostUsd != nil) != (want.Usage.CostUSD != nil) {
				t.Errorf("line %d: a missing limit or cost became a number or the other way round: %v", i, line)
			}
		}
		if msg.OtherRuns != nil && msg.OtherRuns.Runs != uint32(runs-keep) {
			t.Errorf("%d runs: others sum %d runs", runs, msg.OtherRuns.Runs)
		}
		if (msg.Total.CostUsd != nil) != (v.Total.CostUSD != nil) || (msg.LastHourUsd != nil) != (v.LastHour != nil) {
			t.Errorf("%d runs: a missing cost became a number or the other way round", runs)
		}
	}
}

// The host view says its models as a run's view does: the most costly up to the limit, a name cut at a
// character boundary, models_truncated when it left one out, and every figure that is not a list of
// models still counting every turn.
func TestHostCostMessageLimitsModelsAsARunDoes(t *testing.T) {
	var all []hostRun
	for i := range 3 * wire.MaxCostModels {
		spec := core.AgentSpec{Harness: core.Codex, Model: core.ModelName(fmt.Sprintf("model-%03d", i))}
		all = append(all, lineRun(fmt.Sprintf("run-%03d", i), core.RunRunning, 0, spec, core.Usage{CostUSD: dollarsOf(float64(1+i) / 4)}))
	}
	v := buildHost(time.Time{}, all...).View(hostBegin.Add(5 * time.Hour))
	msg := wire.HostCostMessage(v, 1)
	if err := wire.Validate(msg); err != nil {
		t.Fatalf("the contract refuses the message: %v", err)
	}
	if len(msg.Models) != wire.MaxCostModels || !msg.ModelsTruncated || msg.Total.GetCostUsd() != *v.Total.CostUSD {
		t.Fatalf("%d models, truncated %v, total %v", len(msg.Models), msg.ModelsTruncated, msg.Total.GetCostUsd())
	}
	var buckets float64
	for _, b := range msg.Series.Buckets {
		buckets += b.CostUsd
	}
	if buckets != *v.Total.CostUSD {
		t.Fatalf("the buckets hold %v, the total is %v", buckets, *v.Total.CostUSD)
	}
}

const hostCostCorpus = "../../../proto/vibesys/orchestrator/v1/conformance/host_cost/"

// hostCostFrames are the host cost frames the corpus holds, each worked out by the host aggregate from
// runs written out here, so that the corpus is what the host sends and not a hand-written guess.
func hostCostFrames() map[string]*pb.WatchHostCostResponse {
	frame := func(h *cost.Host, now time.Duration) *pb.WatchHostCostResponse {
		return &pb.WatchHostCostResponse{Cost: wire.HostCostMessage(h.View(hostBegin.Add(now)), 41)}
	}
	lead := core.AgentSpec{Harness: core.ClaudeCode}
	worker := core.AgentSpec{Harness: core.Codex, Model: "model-x"}
	live := buildHost(time.Time{},
		lineRun("run-a", core.RunEnded, 500, lead,
			core.Usage{InputTokens: tokensOf(1200), OutputTokens: tokensOf(300), CostUSD: dollarsOf(0.75)},
			core.Usage{OutputTokens: tokensOf(450), CostUSD: dollarsOf(0.5)}),
		lineRun("run-b", core.RunRunning, 0, worker,
			core.Usage{InputTokens: tokensOf(9000), CacheReadTokens: tokensOf(40000), CostUSD: dollarsOf(2.5)},
			core.Usage{InputTokens: tokensOf(700), OutputTokens: tokensOf(90)}),
		lineRun("run-c", core.RunStarting, 0, lead),
	)
	unpriced := buildHost(time.Time{},
		lineRun("run-d", core.RunRunning, 0, worker, core.Usage{InputTokens: tokensOf(5000), OutputTokens: tokensOf(40)}),
	)
	return map[string]*pb.WatchHostCostResponse{
		"empty.json":    frame(cost.NewHost(time.Time{}), 10*time.Minute),
		"live.json":     frame(live, 5*time.Hour),
		"unpriced.json": frame(unpriced, 3*time.Hour),
	}
}

// The corpus is what the host aggregate gives. A change to the view that is meant shows up here as a
// diff of these files; `VIBESYS_UPDATE_GOLDEN=1 go test ./internal/wire -run HostCostCorpus` writes them.
func TestHostCostCorpusIsWhatTheHostGives(t *testing.T) {
	frames := hostCostFrames()
	files, err := filepath.Glob(hostCostCorpus + "*.json")
	if err != nil || len(files) != len(frames) {
		if os.Getenv("VIBESYS_UPDATE_GOLDEN") == "" {
			t.Fatalf("corpus holds %d files, want %d: %v %v", len(files), len(frames), files, err)
		}
	}
	for name, want := range frames {
		t.Run(name, func(t *testing.T) {
			if err := wire.Validate(want); err != nil {
				t.Fatalf("the contract refuses what the host gave: %v", err)
			}
			path := hostCostCorpus + name
			if os.Getenv("VIBESYS_UPDATE_GOLDEN") != "" {
				write(t, path, want)
			}
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			got := new(pb.WatchHostCostResponse)
			if err := protojson.Unmarshal(data, got); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(got, want) {
				t.Fatalf("%s is not what the host gives; rerun with VIBESYS_UPDATE_GOLDEN=1 and review the diff:\n got %v\nwant %v", name, got, want)
			}
		})
	}
}
