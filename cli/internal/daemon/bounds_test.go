package daemon_test

import (
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func declareRequest(objective string, value float64) *cliv1.DeclareBoundRequest {
	return &cliv1.DeclareBoundRequest{
		Context: call, Commit: string(cm(0)), Objective: objective, Value: value, Unit: "ops/s", Note: "bandwidth over bytes per op",
	}
}

// A bound goes through the service as the contract says: declared against the task at a commit,
// recorded with the direction the task gives and the call's origin, never overwritten by a revision,
// and listed oldest first.
func TestABoundIsDeclaredAndListedOverTheService(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)

	first, err := r.client.DeclareBound(ctx, declareRequest("ops", 15000))
	if err != nil {
		t.Fatal(err)
	}
	b := first.GetBound()
	if b.GetSeq() != 1 || b.GetObjective() != "ops" || b.GetDirection() != cliv1.Direction_DIRECTION_MAXIMIZE || b.GetValue() != 15000 ||
		b.GetOrigin().GetRunId() != "run-1" || b.GetOrigin().GetAgent() != "agent-1" || b.GetOrigin().GetAt() == nil {
		t.Fatalf("declared = %v, want place 1, maximize, and the origin of the call", b)
	}
	if _, err := r.client.DeclareBound(ctx, declareRequest("ops", 12000)); err != nil {
		t.Fatal(err)
	}
	listed, err := r.client.ListBounds(ctx, &cliv1.ListBoundsRequest{Context: call})
	if err != nil || len(listed.GetBounds()) != 2 || listed.GetBounds()[0].GetValue() != 15000 || listed.GetBounds()[1].GetValue() != 12000 ||
		listed.GetBounds()[1].GetSeq() != 2 {
		t.Fatalf("listed = %v, %v; want both versions, oldest first", listed, err)
	}
}

func TestABoundIsRefusedWithAnErrorThatNamesWhatIsWrong(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)
	for _, tc := range []struct {
		name string
		req  *cliv1.DeclareBoundRequest
		code codes.Code
		want string
	}{
		{"an objective the task does not declare", declareRequest("goodput", 10), codes.InvalidArgument, "goodput"},
		{"a value of zero", declareRequest("ops", 0), codes.InvalidArgument, "value"},
		{"a negative value", declareRequest("ops", -3), codes.InvalidArgument, "value"},
		{"an empty note", &cliv1.DeclareBoundRequest{Context: call, Commit: string(cm(0)), Objective: "ops", Value: 1, Unit: "u"}, codes.InvalidArgument, "note"},
		{"no unit", &cliv1.DeclareBoundRequest{Context: call, Commit: string(cm(0)), Objective: "ops", Value: 1, Note: "n"}, codes.InvalidArgument, "unit"},
		{"a revision that is not a commit", &cliv1.DeclareBoundRequest{Context: call, Commit: "HEAD", Objective: "ops", Value: 1, Unit: "u", Note: "n"}, codes.InvalidArgument, "commit"},
	} {
		_, err := r.client.DeclareBound(ctx, tc.req)
		if status.Code(err) != tc.code || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: %v, want %s naming %q", tc.name, err, tc.code, tc.want)
		}
		wantKind(t, tc.name, err, core.FailureInvalidInput)
	}
	if listed, err := r.client.ListBounds(ctx, &cliv1.ListBoundsRequest{Context: call}); err != nil || len(listed.GetBounds()) != 0 {
		t.Fatalf("listed = %v, %v; the refused declarations must have recorded nothing", listed, err)
	}
}

// A bound declared after a watch began is told as a fact keyed by its place, without a reset.
func TestWatchTaskTellsOfABoundDeclaredAfterTheReset(t *testing.T) {
	r := newResultsRig(t)
	stream, err := r.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	recvReset(t, stream)
	if _, err := r.client.DeclareBound(ctxFor(t), declareRequest("ops", 15000)); err != nil {
		t.Fatal(err)
	}
	for {
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if m.GetReset_() != nil {
			t.Fatalf("a new bound was sent as a reset: %v", m)
		}
		for _, f := range m.GetFacts().GetFacts() {
			if b := f.GetBound(); b != nil && b.GetSeq() == 1 && b.GetObjective() == "ops" {
				return
			}
		}
	}
}
