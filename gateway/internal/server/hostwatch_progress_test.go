package server

import (
	"context"
	"errors"
	"math/rand"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// A stream is failed by silence, not by length: these tests run the Watch handler against a
// program of frames and gaps in a synctest bubble, whose clock moves only when everything waits,
// so no test measures time and none can flake.

const listingRequestID = "listing-a"

func listingRequest() *apiv1.WatchRequest {
	return &apiv1.WatchRequest{
		Checkout: "/checkout",
		Method: &apiv1.WatchRequest_StreamSavedRuns{StreamSavedRuns: &hostv1.StreamSavedRunsRequest{
			RequestId: listingRequestID, Project: "/work/p",
		}},
	}
}

func listingFrame(t *testing.T, complete bool) []byte {
	t.Helper()
	payload, err := proto.Marshal(&apiv1.WatchResponse{Result: &apiv1.WatchResponse_StreamSavedRuns{
		StreamSavedRuns: &hostv1.StreamSavedRunsResponse{RequestId: listingRequestID, ProjectRoot: "/work/p", Complete: complete},
	}})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

// program waits gap before each frame and ends the way ending says.
type program struct {
	gaps   []time.Duration
	ending func() error
}

func (p program) runner(t *testing.T) hostcommand.FakeRunner {
	return hostcommand.FakeRunner{Frames: func(ctx context.Context, _ hostcommand.Command, emit func([]byte) error) error {
		for index, gap := range p.gaps {
			timer := time.NewTimer(gap)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
			if err := emit(listingFrame(t, index == len(p.gaps)-1)); err != nil {
				return err
			}
		}
		if p.ending != nil {
			return p.ending()
		}
		return nil
	}}
}

// watch runs the program through Watch in a bubble and returns what the client was sent.
func watch(t *testing.T, p program) []*apiv1.WatchResponse {
	t.Helper()
	var sent []*apiv1.WatchResponse
	synctest.Test(t, func(t *testing.T) {
		service := &host{config: Config{Root: t.TempDir(), HostRunner: p.runner(t)}}
		stream := &watchStream{ctx: context.Background()}
		if err := service.Watch(listingRequest(), stream); err != nil {
			t.Fatal(err)
		}
		sent = stream.sent
	})
	return sent
}

func listing(t *testing.T) hostoperations.HostCall {
	t.Helper()
	dispatched, ok := hostoperations.DispatchHostWatch(listingRequest())
	if !ok || dispatched.Progress <= 0 || dispatched.FirstEvent <= dispatched.Progress {
		t.Fatalf("the listing declares no progress bound, or no first-frame window past it: %+v", dispatched)
	}
	return dispatched
}

func failureOf(t *testing.T, sent []*apiv1.WatchResponse) *hostv1.OperationFailure {
	t.Helper()
	if len(sent) == 0 || sent[len(sent)-1].GetFailure() == nil {
		t.Fatalf("the stream did not end in a failure: %+v", sent)
	}
	return sent[len(sent)-1].GetFailure()
}

// Whatever its length, a stream whose every gap is under the bound is delivered whole, with no failure.
func TestAStreamWithNoLongSilenceIsDeliveredWholeHoweverLongItRuns(t *testing.T) {
	dispatched := listing(t)
	longer := 0
	for seed := int64(0); seed < 60; seed++ {
		random := rand.New(rand.NewSource(seed))
		gaps := make([]time.Duration, 1+random.Intn(40))
		var total time.Duration
		for index := range gaps {
			gaps[index] = time.Duration(random.Int63n(int64(dispatched.Progress)))
			// The total stays under the hang guard; only silence is being tested.
			if total+gaps[index] >= dispatched.Deadline {
				gaps = gaps[:index]
				break
			}
			total += gaps[index]
		}
		if len(gaps) == 0 {
			continue
		}
		if total > dispatched.Progress {
			longer++
		}
		sent := watch(t, program{gaps: gaps})
		if len(sent) != len(gaps) {
			t.Fatalf("seed %d: %d of %d frames arrived, after %s of gaps: %+v", seed, len(sent), len(gaps), total, sent)
		}
		for _, response := range sent {
			if response.GetFailure() != nil {
				t.Fatalf("seed %d: a stream with every gap under %s failed: %v", seed, dispatched.Progress, response.GetFailure())
			}
		}
		if !sent[len(sent)-1].GetStreamSavedRuns().GetComplete() {
			t.Fatalf("seed %d: the last frame does not say complete", seed)
		}
	}
	if longer < 30 {
		t.Fatalf("only %d of the programs ran longer than the bound %s; the property was not exercised", longer, dispatched.Progress)
	}
}

// One gap that outlasts the bound ends the stream there, whatever came before it. The gap before
// the first frame is held to the first-frame window, every later gap to the progress bound.
func TestAGapPastTheBoundFailsTheStreamAndKeepsTheFramesBeforeIt(t *testing.T) {
	dispatched := listing(t)
	for seed := int64(0); seed < 40; seed++ {
		random := rand.New(rand.NewSource(seed))
		before := random.Intn(12)
		gaps := make([]time.Duration, before, before+2)
		for index := range gaps {
			gaps[index] = time.Duration(random.Int63n(int64(dispatched.Progress)))
		}
		window := dispatched.Progress
		if before == 0 {
			window = dispatched.FirstEvent
		}
		gaps = append(gaps, window+time.Nanosecond+time.Duration(random.Int63n(int64(time.Minute))), time.Second)
		sent := watch(t, program{gaps: gaps})
		if len(sent) != before+1 {
			t.Fatalf("seed %d: %d frames arrived, want the %d before the silence and a failure: %+v", seed, len(sent), before, sent)
		}
		for _, response := range sent[:before] {
			if response.GetStreamSavedRuns() == nil {
				t.Fatalf("seed %d: a frame before the silence was lost: %+v", seed, sent)
			}
		}
		failure := failureOf(t, sent)
		if failure.GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED {
			t.Fatalf("seed %d: category %v", seed, failure.GetCategory())
		}
		if want := silenceMessage("watch", dispatched.CLI, window); failure.GetMessage() != want {
			t.Fatalf("seed %d: message %q, want %q", seed, failure.GetMessage(), want)
		}
	}
}

func TestAStreamSilentBeforeItsFirstFrameFailsNamingTheOperation(t *testing.T) {
	dispatched := listing(t)
	sent := watch(t, program{gaps: []time.Duration{dispatched.FirstEvent + time.Nanosecond, time.Second}})
	if len(sent) != 1 {
		t.Fatalf("sent %+v", sent)
	}
	failure := failureOf(t, sent)
	if failure.GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED || !strings.Contains(failure.GetMessage(), dispatched.CLI) {
		t.Fatalf("failure %v does not name %q", failure, dispatched.CLI)
	}
	if want := silenceMessage("watch", dispatched.CLI, dispatched.FirstEvent); failure.GetMessage() != want {
		t.Fatalf("message %q, want %q", failure.GetMessage(), want)
	}
}

// The first frame pays for the process start, the repository open and the branch listing, so a
// wait past the progress bound that ends inside the first-frame window is not silence.
func TestTheFirstFrameMayTakeLongerThanTheGapsAfterIt(t *testing.T) {
	dispatched := listing(t)
	for _, first := range []time.Duration{dispatched.Progress + time.Nanosecond, dispatched.FirstEvent - time.Nanosecond} {
		sent := watch(t, program{gaps: []time.Duration{first, time.Second}})
		if len(sent) != 2 || sent[0].GetFailure() != nil || sent[1].GetFailure() != nil {
			t.Fatalf("a first frame after %s: sent %+v", first, sent)
		}
	}
}

// The total cap is a hang guard and is still enforced: a stream that is never silent but never ends is cut.
func TestAStreamThatNeverEndsIsStillCutByTheHangGuard(t *testing.T) {
	dispatched := listing(t)
	gaps := make([]time.Duration, int(dispatched.Deadline/time.Second)+30)
	for index := range gaps {
		gaps[index] = time.Second
	}
	sent := watch(t, program{gaps: gaps})
	failure := failureOf(t, sent)
	if want := deadlineMessage("watch", dispatched.CLI, dispatched.Deadline); failure.GetMessage() != want {
		t.Fatalf("message %q, want %q", failure.GetMessage(), want)
	}
	if len(sent) < 2 {
		t.Fatalf("the frames before the cap were lost: %d", len(sent))
	}
}

// A finite stream that dies without its completing frame says so after the frames it sent.
func TestAFiniteStreamThatDiesAfterFramesEndsInAFailure(t *testing.T) {
	dispatched := listing(t)
	sent := watch(t, program{
		gaps:   []time.Duration{time.Second, time.Second},
		ending: func() error { return errors.New("exit status 1") },
	})
	if len(sent) != 3 {
		t.Fatalf("sent %+v", sent)
	}
	failure := failureOf(t, sent)
	if failure.GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(failure.GetMessage(), dispatched.CLI) {
		t.Fatalf("failure %v", failure)
	}
}

// Login declares no progress bound: after its first event it may stay quiet until its cap.
func TestAStreamWithoutAProgressBoundMayStayQuietAfterItsFirstEvent(t *testing.T) {
	var sent []*apiv1.WatchResponse
	started, err := proto.Marshal(&apiv1.WatchResponse{Result: &apiv1.WatchResponse_Login{Login: &hostv1.LoginResponse{
		RequestId: "request-a", Sequence: 1,
		Body: &hostv1.LoginResponse_Started{Started: &hostv1.LoginStartedBody{Instruction: proto.String("Open https://a.example/x")}},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	synctest.Test(t, func(t *testing.T) {
		service := &host{config: Config{Root: t.TempDir(), HostRunner: hostcommand.FakeRunner{Frames: func(ctx context.Context, _ hostcommand.Command, emit func([]byte) error) error {
			if err := emit(started); err != nil {
				return err
			}
			time.Sleep(5 * time.Minute)
			return nil
		}}}}
		stream := &watchStream{ctx: context.Background()}
		request := &apiv1.WatchRequest{
			Checkout: "/checkout",
			Method:   &apiv1.WatchRequest_Login{Login: &hostv1.LoginRequest{RequestId: "request-a", Provider: "claude"}},
		}
		if err := service.Watch(request, stream); err != nil {
			t.Fatal(err)
		}
		sent = stream.sent
	})
	if len(sent) != 1 || sent[0].GetLogin() == nil {
		t.Fatalf("sent %+v", sent)
	}
}
