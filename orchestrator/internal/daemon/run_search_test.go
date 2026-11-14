package daemon_test

import (
	"context"
	"fmt"
	"slices"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

func startSearchRun(t *testing.T, rig *daemontest.Rig, n int, task string, card core.TaskCard) (core.Run, core.Agent) {
	t.Helper()
	rig.Tasks.Set(task, card)
	reply, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: fmt.Sprintf("start-search-%d", n),
		Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
			Repository:  "/repo/" + task,
			Task:        task,
			FanoutLimit: 1,
			Brief:       "go",
			Lead:        &pb.AgentSpec{Harness: pb.Harness_HARNESS_CODEX, Model: "gpt-" + task},
		}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	run, ok := rig.Daemon.State().Run(core.RunID(reply.GetReceipt().GetRunId()))
	if !ok {
		t.Fatalf("started run %q is absent", reply.GetReceipt().GetRunId())
	}
	lead, ok := rig.Daemon.State().Agent(run.Lead)
	if !ok {
		t.Fatalf("lead %q is absent", run.Lead)
	}
	return run, lead
}

func searchRuns(t *testing.T, rig *daemontest.Rig, query string) *pb.SearchRunsResponse {
	t.Helper()
	resp, err := rig.Service.SearchRuns(context.Background(), &pb.SearchRunsRequest{Query: query})
	if err != nil {
		t.Fatal(err)
	}
	if err := wire.Validate(resp); err != nil {
		t.Fatalf("response is not valid: %v", err)
	}
	if size := proto.Size(resp); size > daemon.MaxRunSearchBytes {
		t.Fatalf("response is %d bytes, above its %d byte budget", size, daemon.MaxRunSearchBytes)
	}
	return resp
}

func resultIDs(resp *pb.SearchRunsResponse) []string {
	return slices.Collect(func(yield func(string) bool) {
		for _, result := range resp.GetResults() {
			if !yield(result.GetRun().GetId()) {
				return
			}
		}
	})
}

// The public read searches the semantic facts the Runs screen promises, including full CLI plan and
// workstream records rather than narration, and every query word must match somewhere in one run.
func TestSearchRunsFindsEveryRegisteredMeaning(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := startSearchRun(t, rig, 1, "routing", core.TaskCard{
		Task:                "routing",
		Heading:             "Café vector routing",
		WorkloadName:        "gateway",
		WorkloadDescription: "Route semantic traffic",
		HardwareName:        "CPU host",
		ModelName:           "Llama 3",
		Objectives:          []core.CardObjective{{Name: "latency", Direction: core.Minimize, Unit: "milliseconds"}},
	})
	other, _ := startSearchRun(t, rig, 2, "unrelated", core.TaskCard{Task: "unrelated", Heading: "Other work"})
	store := new(search.Store)
	store.Put(run.ID, search.Fold(search.Facts{
		{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Title: "Radix cache", Body: "Reuse prefix blocks"}}},
		{Fact: &cli.TaskFact_Workstream{Workstream: &cli.Workstream{Number: 1, Title: "Allocator lane", Note: "testing slab reuse"}}},
	}))
	rig.Search, rig.Daemon.Search = store, store
	post(t, rig, lead.ID, "search-report", reportCommand(pb.ReportKind_REPORT_KIND_PROGRESS, "Index ready"))

	queries := []string{
		"cafe", "semantic", "llama", "gpt-routing", "latency milliseconds",
		"repo routing", string(run.ID), "radix prefix", "allocator slab", "index ready",
	}
	for _, query := range queries {
		t.Run(query, func(t *testing.T) {
			got := resultIDs(searchRuns(t, rig, query))
			if !slices.Equal(got, []string{string(run.ID)}) {
				t.Fatalf("SearchRuns(%q) = %v, want only %s (other run %s)", query, got, run.ID, other.ID)
			}
		})
	}
	if got, want := resultIDs(searchRuns(t, rig, "openai")), []string{string(run.ID), string(other.ID)}; !slices.Equal(got, want) {
		t.Fatalf("the harness alias matched %v, want both Codex runs %v", got, want)
	}
	if got := searchRuns(t, rig, "semantic absent").GetResults(); len(got) != 0 {
		t.Fatalf("a conjunctive query with one absent word returned %v", resultIDs(&pb.SearchRunsResponse{Results: got}))
	}
}

// Relevance is stable and useful: an exact title outranks a model occurrence, while equal matches
// are newest first. Every result is a run/summary pair from the response's one sequence cut.
func TestSearchRunsRanksAndReturnsAuthoritativePairs(t *testing.T) {
	rig := daemontest.NewRig(t)
	older, _ := startSearchRun(t, rig, 1, "needle", core.TaskCard{Task: "needle", Heading: "Needle"})
	rig.Clock.Advance(1)
	newer, _ := startSearchRun(t, rig, 2, "newer", core.TaskCard{Task: "newer", ModelName: "needle"})
	rig.Clock.Advance(1)
	newest, _ := startSearchRun(t, rig, 3, "newest", core.TaskCard{Task: "newest", ModelName: "needle"})

	resp := searchRuns(t, rig, "needle")
	if got, want := resultIDs(resp), []string{string(older.ID), string(newest.ID), string(newer.ID)}; !slices.Equal(got, want) {
		t.Fatalf("ranked ids %v, want %v", got, want)
	}
	if resp.GetSequence() != rig.Daemon.Last() {
		t.Fatalf("response sequence %d, want current %d", resp.GetSequence(), rig.Daemon.Last())
	}
	for _, result := range resp.GetResults() {
		if result.GetSummary().GetRunId() != result.GetRun().GetId() {
			t.Fatalf("mismatched result: %v", result)
		}
	}
}

func TestSearchRunsIsBoundedAndOperatorOnly(t *testing.T) {
	rig := daemontest.NewRig(t)
	var lead core.Agent
	for i := range daemon.MaxRunSearchResults + 4 {
		_, lead = startSearchRun(t, rig, i, fmt.Sprintf("common-%02d", i), core.TaskCard{Task: fmt.Sprintf("common-%02d", i), Heading: "common"})
	}
	resp := searchRuns(t, rig, "common")
	if len(resp.GetResults()) != daemon.MaxRunSearchResults || !resp.GetTruncated() {
		t.Fatalf("got %d results, truncated %t; want %d and true", len(resp.GetResults()), resp.GetTruncated(), daemon.MaxRunSearchResults)
	}

	token := rig.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Credentials.GetToken()
	_, err := rig.AgentService.SearchRuns(agentContext(lead.ID, 1, token), &pb.SearchRunsRequest{Query: "common"})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("agent SearchRuns gave %v, want PermissionDenied", err)
	}
	for _, query := range []string{"", "   ---"} {
		_, err := rig.Service.SearchRuns(context.Background(), &pb.SearchRunsRequest{Query: query})
		if status.Code(err) != codes.InvalidArgument {
			t.Errorf("query %q gave %v, want InvalidArgument", query, err)
		}
	}

	large := daemontest.NewRig(t)
	for i := range 14 {
		_, runLead := startSearchRun(t, large, i, fmt.Sprintf("large-common-%02d", i), core.TaskCard{
			Task: fmt.Sprintf("large-common-%02d", i), Heading: "large common",
		})
		post(t, large, runLead.ID, fmt.Sprintf("large-report-%d", i), bigReport(pb.ReportKind_REPORT_KIND_PROGRESS, 240<<10))
	}
	byBytes := searchRuns(t, large, "large common")
	if len(byBytes.GetResults()) >= 14 || !byBytes.GetTruncated() {
		t.Fatalf("byte-bounded search returned %d of 14 results, truncated %t; want fewer and true", len(byBytes.GetResults()), byBytes.GetTruncated())
	}
}
