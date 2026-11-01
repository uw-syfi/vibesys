package daemon_test

import (
	"context"
	"math"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The result calls are served by thin handlers: what they answer is what app decides, translated.
// These tests drive them through the gRPC interface and check the translation and the status of
// each answer: a refusal is an OK response, and every failure is a status that carries a typed
// Failure.

func cm(n int) core.Commit {
	return core.Commit(string(rune('a'+n)) + "000000000000000000000000000000000000000")[:40]
}

type resultsRig struct {
	t        *testing.T
	client   cliv1.DaemonServiceClient
	branches *gitrepo.FakeBranches
	clk      *clock.Manual
	app      *app.App
	stores   *app.MemoryStores
}

func newResultsRig(t *testing.T) *resultsRig {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	env := fakeenv.New(core.EnvironmentHost, gates)
	envs := fakeenv.Environments{core.EnvironmentHost: env}
	branches := gitrepo.NewFakeBranches()
	branches.AddCommit("/repo", cm(0))
	branches.AddCommit("/repo", cm(1), cm(0))
	branches.AddCommit("/repo", cm(2), cm(0))
	branches.AddBranch("/repo", "vibesys/result/task", cm(0))
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	stores := app.NewMemoryStores(app.Rules(5))
	survivors := supervisor.NewSurvivors()
	a := app.New(app.Config{
		Clock: clk, Stores: stores, Environments: envs, Branches: branches,
		Supervisors: supervisor.NewInProcess(envs, &fakeenv.Checkouts{}, survivors),
	})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test")
	listener := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	cliv1.RegisterDaemonServiceServer(gs, srv)
	go gs.Serve(listener)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		conn.Close()
		srv.Shutdown()
		gs.Stop()
		a.Close()
		survivors.Close()
	})
	return &resultsRig{t: t, client: cliv1.NewDaemonServiceClient(conn), branches: branches, clk: clk, app: a, stores: stores}
}

func (r *resultsRig) measured(c core.Commit, samples ...float64) {
	r.t.Helper()
	st, err := r.stores.Open(ctxFor(r.t), app.Scope{Repository: "/repo", Task: "task"})
	if err != nil {
		r.t.Fatal(err)
	}
	storetest.SeedMeasurement(r.t, st, storetest.Measurement{Commit: c, Samples: samples, At: r.clk.Now()})
}

var (
	winning = []float64{120, 121, 119, 122, 120}
	levelAt = []float64{100, 101, 99, 100, 102}
)

func TestPromoteAnswersARefusalAsAResponseAndAPromotionAsAnother(t *testing.T) {
	r := newResultsRig(t)
	r.measured(cm(0), levelAt...)
	r.measured(cm(1), winning...)
	r.measured(cm(2), 80, 81, 79, 80, 82)

	refused, err := r.client.Promote(ctxFor(t), &cliv1.PromoteRequest{Context: call, Commit: string(cm(2))})
	if err != nil || refused.GetRefused() == nil || len(refused.GetRefused().GetReasons()) != 1 ||
		refused.GetRefused().GetReasons()[0].GetKind() != cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_WORSE ||
		refused.GetRefused().GetReasons()[0].GetVerdict().GetKind() != cliv1.VerdictKind_VERDICT_KIND_WORSE {
		t.Fatalf("Promote of a worse commit = %+v, %v; want a refusal response carrying the verdict", refused, err)
	}
	if at, _ := r.branches.At("/repo", "vibesys/result/task"); at != cm(0) {
		t.Fatalf("a refusal moved the branch to %s", at)
	}

	promoted, err := r.client.Promote(ctxFor(t), &cliv1.PromoteRequest{Context: call, Commit: string(cm(1))})
	if err != nil || promoted.GetPromoted().GetCommit() != string(cm(1)) || promoted.GetPromoted().GetPreviousHead() != string(cm(0)) ||
		promoted.GetPromoted().GetVerdict().GetKind() != cliv1.VerdictKind_VERDICT_KIND_BETTER {
		t.Fatalf("Promote of a better commit = %+v, %v", promoted, err)
	}
	if at, _ := r.branches.At("/repo", "vibesys/result/task"); at != cm(1) {
		t.Fatalf("the branch is at %s", at)
	}
}

// Every way a result call can fail is a gRPC status with a typed Failure, and the status code
// agrees with the kind.
func TestResultCallsFailWithTypedFailures(t *testing.T) {
	r := newResultsRig(t)
	r.measured(cm(0), levelAt...)
	r.measured(cm(1), winning...)
	ctx := ctxFor(t)
	other := &cliv1.CallContext{Repository: "/repo", Task: "unmapped"}

	cases := []struct {
		name string
		err  func() error
		kind core.FailureKind
		code codes.Code
	}{
		{"promote of a revision", func() error {
			_, err := r.client.Promote(ctx, &cliv1.PromoteRequest{Context: call, Commit: "main"})
			return err
		}, core.FailureInvalidInput, codes.InvalidArgument},
		{"promote without a result branch", func() error {
			_, err := r.client.Promote(ctx, &cliv1.PromoteRequest{Context: other, Commit: string(cm(1))})
			return err
		}, core.FailureNotFound, codes.NotFound},
		{"compare of an unmeasured commit", func() error {
			_, err := r.client.Compare(ctx, &cliv1.CompareRequest{Context: call, Commit: string(cm(2))})
			return err
		}, core.FailureNotMeasured, codes.FailedPrecondition},
		{"show without a task", func() error {
			_, err := r.client.Show(ctx, &cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: "/repo"}, Commit: string(cm(1))})
			return err
		}, core.FailureInvalidInput, codes.InvalidArgument},
		{"result without a repository", func() error {
			_, err := r.client.CurrentResult(ctx, &cliv1.CurrentResultRequest{Context: &cliv1.CallContext{Task: "task"}})
			return err
		}, core.FailureInvalidInput, codes.InvalidArgument},
	}
	for _, tc := range cases {
		err := tc.err()
		f, ok := wire.FailureOf(err)
		if !ok || f.Kind != tc.kind || f.Message == "" || status.Code(err) != tc.code {
			t.Errorf("%s: %v (failure %+v, ok %v); want kind %s with code %s", tc.name, err, f, ok, tc.kind, tc.code)
		}
	}
}

// Numbers cross the wire exactly: a comparison's ratio and the samples of a shown measurement
// come back as the same doubles.
func TestComparisonAndShowCarryExactNumbers(t *testing.T) {
	r := newResultsRig(t)
	r.measured(cm(0), levelAt...)
	odd := []float64{math.Pi * 40, math.E * 44, 120.1, 119.9, 121.3}
	r.measured(cm(1), odd...)

	resp, err := r.client.Compare(ctxFor(t), &cliv1.CompareRequest{Context: call, Commit: string(cm(1))})
	if err != nil {
		t.Fatal(err)
	}
	want, err := core.Compare(core.CompareInput{Direction: core.Maximize, Candidate: odd, Base: levelAt})
	if err != nil {
		t.Fatal(err)
	}
	v := resp.GetComparison().GetVerdict()
	if math.Float64bits(v.GetRatio()) != math.Float64bits(want.Ratio) || resp.GetComparison().GetAgainst() != string(cm(0)) {
		t.Fatalf("verdict %+v against %s, want ratio %v against %s", v, resp.GetComparison().GetAgainst(), want.Ratio, cm(0))
	}

	shown, err := r.client.Show(ctxFor(t), &cliv1.ShowRequest{Context: call, Commit: string(cm(1))})
	if err != nil || len(shown.GetResults()) != 1 {
		t.Fatalf("Show = %+v, %v", shown, err)
	}
	for i, s := range shown.GetResults()[0].GetMeasure().GetSamples() {
		if math.Float64bits(s) != math.Float64bits(odd[i]) {
			t.Fatalf("sample %d is %v, want %v", i, s, odd[i])
		}
	}
}

func TestCurrentResultAnswersNoneThenTheNumber(t *testing.T) {
	r := newResultsRig(t)
	first, err := r.client.CurrentResult(ctxFor(t), &cliv1.CurrentResultRequest{Context: call})
	if err != nil || first.GetNone().GetReason() != cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NO_HEAD {
		t.Fatalf("result = %+v, %v", first, err)
	}
	r.measured(cm(0), levelAt...)
	r.measured(cm(1), winning...)
	if _, err := r.client.Promote(ctxFor(t), &cliv1.PromoteRequest{Context: call, Commit: string(cm(1))}); err != nil {
		t.Fatal(err)
	}
	second, err := r.client.CurrentResult(ctxFor(t), &cliv1.CurrentResultRequest{Context: call})
	if err != nil || second.GetNone().GetReason() != cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NOT_MEASURED_SINCE_PROMOTION ||
		second.GetNone().GetHead() != string(cm(1)) {
		t.Fatalf("result after a promotion = %+v, %v", second, err)
	}
	r.measured(cm(1), 119, 120, 121, 118, 120)
	third, err := r.client.CurrentResult(ctxFor(t), &cliv1.CurrentResultRequest{Context: call})
	if err != nil || third.GetCurrent().GetHead() != string(cm(1)) || len(third.GetCurrent().GetResult().GetMeasure().GetSamples()) != 5 {
		t.Fatalf("result after a fresh measurement = %+v, %v", third, err)
	}
}
