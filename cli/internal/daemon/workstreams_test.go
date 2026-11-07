package daemon_test

import (
	"fmt"
	"strings"
	"sync"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// A workstream goes through the service as the contract says: started with the title as the agent
// wrote it, found again by the same title in any case and spacing, edited field by field, listed by
// number, and joined by plans that name it.
func TestAWorkstreamIsStartedFoundEditedAndJoinedOverTheService(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	r.taskWithAJob()

	started, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: "  KV   cache layout "})
	if err != nil {
		t.Fatal(err)
	}
	w := started.GetWorkstream()
	if !started.GetCreated() || w.GetNumber() != 1 || w.GetTitle() != "KV cache layout" || w.GetStatus() != cliv1.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE ||
		w.GetOrigin().GetRunId() != "run-1" || w.GetOrigin().GetAgent() != "agent-1" || !w.GetUpdatedAt().IsValid() {
		t.Fatalf("started = %v, want workstream 1, title collapsed, active, with the origin of the call", started)
	}
	again, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: "kv CACHE layout"})
	if err != nil || again.GetCreated() || !proto.Equal(again.GetWorkstream(), w) {
		t.Fatalf("starting the same title = %v, %v; want the workstream found, not created", again, err)
	}

	edited, err := r.client.EditWorkstream(ctx, &cliv1.EditWorkstreamRequest{
		Context: call, Number: 1, Status: cliv1.WorkstreamStatus_WORKSTREAM_STATUS_PAUSED.Enum(), Note: proto.String("paused: accuracy gate"),
	})
	if err != nil || edited.GetWorkstream().GetStatus() != cliv1.WorkstreamStatus_WORKSTREAM_STATUS_PAUSED ||
		edited.GetWorkstream().GetNote() != "paused: accuracy gate" || edited.GetWorkstream().GetTitle() != "KV cache layout" {
		t.Fatalf("edited = %v, %v; want paused with the note and the title kept", edited, err)
	}
	if _, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: "Batching"}); err != nil {
		t.Fatal(err)
	}
	listed, err := r.client.ListWorkstreams(ctx, &cliv1.ListWorkstreamsRequest{Context: call})
	if err != nil || len(listed.GetWorkstreams()) != 2 || listed.GetWorkstreams()[0].GetNumber() != 1 || listed.GetWorkstreams()[1].GetNumber() != 2 {
		t.Fatalf("listed = %v, %v; want both, by number", listed, err)
	}
	paused, err := r.client.ListWorkstreams(ctx, &cliv1.ListWorkstreamsRequest{Context: call, Statuses: []cliv1.WorkstreamStatus{cliv1.WorkstreamStatus_WORKSTREAM_STATUS_PAUSED}})
	if err != nil || len(paused.GetWorkstreams()) != 1 || paused.GetWorkstreams()[0].GetNumber() != 1 {
		t.Fatalf("paused = %v, %v; want workstream 1", paused, err)
	}

	plan, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: "page the cache", Workstream: 1})
	if err != nil || plan.GetPlan().GetWorkstream() != 1 {
		t.Fatalf("a plan in workstream 1 = %v, %v", plan, err)
	}
	moved, err := r.client.EditPlan(ctx, &cliv1.EditPlanRequest{Context: call, Number: plan.GetPlan().GetNumber(), Workstream: 2})
	if err != nil || moved.GetPlan().GetWorkstream() != 2 {
		t.Fatalf("a plan moved to workstream 2 = %v, %v", moved, err)
	}
	kept, err := r.client.EditPlan(ctx, &cliv1.EditPlanRequest{Context: call, Number: plan.GetPlan().GetNumber(), Title: proto.String("renamed")})
	if err != nil || kept.GetPlan().GetWorkstream() != 2 {
		t.Fatalf("an edit that names no workstream = %v, %v; want the plan left in workstream 2", kept, err)
	}
}

// A workstream a plan names that the task does not have is NOT_FOUND, and the message names the flag
// that gave it, on create and on edit, whatever the number.
func TestAnUnknownWorkstreamIsNotFoundAndNamesTheFlag(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	r.taskWithAJob()
	if _, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: "KV cache layout"}); err != nil {
		t.Fatal(err)
	}
	plan, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: "t"})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range []uint32{2, 3, 99, 1 << 31} {
		_, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: "t", Workstream: n})
		wantKind(t, fmt.Sprint("CreatePlan in workstream ", n), err, core.FailureNotFound)
		if status.Code(err) != codes.NotFound || !strings.Contains(err.Error(), "--workstream") || !strings.Contains(err.Error(), fmt.Sprint(n)) {
			t.Errorf("CreatePlan in workstream %d: %v, want NOT_FOUND naming --workstream and the number", n, err)
		}
		_, err = r.client.EditPlan(ctx, &cliv1.EditPlanRequest{Context: call, Number: plan.GetPlan().GetNumber(), Workstream: n})
		wantKind(t, fmt.Sprint("EditPlan to workstream ", n), err, core.FailureNotFound)
		if !strings.Contains(err.Error(), "--workstream") {
			t.Errorf("EditPlan to workstream %d: %v does not name --workstream", n, err)
		}
	}
	list, err := r.client.ListPlans(ctx, &cliv1.ListPlansRequest{Context: call})
	if err != nil || len(list.GetPlans()) != 1 {
		t.Fatalf("plans = %v, %v; the refused calls must have made none", list, err)
	}
	_, err = r.client.EditWorkstream(ctx, &cliv1.EditWorkstreamRequest{Context: call, Number: 9, Note: proto.String("n")})
	wantKind(t, "EditWorkstream of an unknown workstream", err, core.FailureNotFound)
}

func TestWorkstreamCallsRefuseWhatIsNotAWorkstream(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	r.taskWithAJob()
	for name, title := range map[string]string{"a blank title": "   ", "a title of two lines": "a\nb", "a title too long": strings.Repeat("x", 81)} {
		_, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: title})
		wantKind(t, "StartWorkstream with "+name, err, core.FailureInvalidInput)
	}
	if _, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: "KV cache"}); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []cliv1.WorkstreamStatus{cliv1.WorkstreamStatus_WORKSTREAM_STATUS_UNSPECIFIED, cliv1.WorkstreamStatus(42)} {
		_, err := r.client.EditWorkstream(ctx, &cliv1.EditWorkstreamRequest{Context: call, Number: 1, Status: bad.Enum()})
		wantKind(t, "EditWorkstream", err, core.FailureInvalidInput)
		_, err = r.client.ListWorkstreams(ctx, &cliv1.ListWorkstreamsRequest{Context: call, Statuses: []cliv1.WorkstreamStatus{bad}})
		wantKind(t, "ListWorkstreams", err, core.FailureInvalidInput)
	}
	_, err := r.client.EditWorkstream(ctx, &cliv1.EditWorkstreamRequest{Context: call, Number: 1})
	wantKind(t, "an EditWorkstream that sets nothing", err, core.FailureInvalidInput)
	_, err = r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: &cliv1.CallContext{Repository: "/repo"}, Title: "t"})
	wantKind(t, "StartWorkstream with no task", err, core.FailureInvalidInput)
}

// Planners that start one line of work at the same moment, each in its own spelling, get one
// workstream: one call creates it and the others find it.
func TestParallelStartsOfOneWorkstreamMakeOne(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	r.taskWithAJob()
	spellings := []string{"KV cache layout", "kv cache layout", "  KV   cache\tlayout ", "Kv CACHE Layout"}
	const n = 12
	var mu sync.Mutex
	var numbers []uint32
	created := 0
	storetest.Parallel(t, n, func(i int) {
		resp, err := r.client.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: call, Title: spellings[i%len(spellings)]})
		if err != nil {
			t.Errorf("StartWorkstream: %v", err)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		numbers = append(numbers, resp.GetWorkstream().GetNumber())
		if resp.GetCreated() {
			created++
		}
	})
	for _, number := range numbers {
		if number != 1 {
			t.Fatalf("numbers = %v, want 1 for every start", numbers)
		}
	}
	if created != 1 || len(numbers) != n {
		t.Fatalf("%d of %d starts created a workstream, want exactly 1", created, len(numbers))
	}
}

// A workstream started after a watch began is told as a fact keyed by its number, without a reset,
// and so is each change to it.
func TestWatchTaskTellsOfAWorkstreamStartedAndEditedAfterTheReset(t *testing.T) {
	r := newRig(t)
	r.taskWithAJob()
	stream, err := r.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	recvReset(t, stream)
	if _, err := r.client.StartWorkstream(ctxFor(t), &cliv1.StartWorkstreamRequest{Context: call, Title: "KV cache layout"}); err != nil {
		t.Fatal(err)
	}
	next := func(match func(*cliv1.Workstream) bool) {
		t.Helper()
		for {
			m, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			if m.GetReset_() != nil {
				t.Fatalf("a workstream was sent as a reset: %v", m)
			}
			for _, f := range m.GetFacts().GetFacts() {
				if w := f.GetWorkstream(); w != nil && match(w) {
					return
				}
			}
		}
	}
	next(func(w *cliv1.Workstream) bool { return w.GetNumber() == 1 && w.GetTitle() == "KV cache layout" })
	if _, err := r.client.EditWorkstream(ctxFor(t), &cliv1.EditWorkstreamRequest{Context: call, Number: 1, Note: proto.String("paused: accuracy gate")}); err != nil {
		t.Fatal(err)
	}
	next(func(w *cliv1.Workstream) bool { return w.GetNumber() == 1 && w.GetNote() == "paused: accuracy gate" })
	if _, err := r.client.CreatePlan(ctxFor(t), &cliv1.CreatePlanRequest{Context: call, Title: "t", Workstream: 1}); err != nil {
		t.Fatal(err)
	}
	for {
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range m.GetFacts().GetFacts() {
			if p := f.GetPlan(); p != nil && p.GetWorkstream() == 1 {
				return
			}
		}
	}
}
