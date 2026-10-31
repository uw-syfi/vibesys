package subscription_test

import (
	"context"
	"errors"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/quick"
	"testing/synctest"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type stats = *apiv1.MachineStats

var (
	request = &apiv1.SubscribeRequest{Topic: &apiv1.SubscribeRequest_MachineStats{MachineStats: &apiv1.MachineStatsTopic{}}}
	policy  = hostoperations.TopicPolicy{
		Cadence: time.Second, StaleAfter: 3 * time.Second, FirstFrame: time.Second, MaxKeys: 2, MaxValueBytes: 1024,
	}
)

// value is a schema-valid message whose content is its second.
func value(second int64) stats {
	absent := &apiv1.Absence{Reason: apiv1.AbsenceReason_ABSENCE_REASON_TOOL_NOT_FOUND}
	return &apiv1.MachineStats{
		SampledAt: &timestamppb.Timestamp{Seconds: second},
		Cpu:       &apiv1.MachineStats_CpuUnavailable{CpuUnavailable: absent},
		Memory:    &apiv1.MachineStats_MemoryUnavailable{MemoryUnavailable: absent},
		Gpu:       &apiv1.MachineStats_GpuUnavailable{GpuUnavailable: absent},
	}
}

func topic(source subscription.Source[stats]) subscription.Topic[stats] {
	return subscription.Topic[stats]{
		Name: hostoperations.TopicMachineStats, Policy: policy, Source: source,
		Entry: func(key string, message stats) *apiv1.TopicEntry {
			return &apiv1.TopicEntry{Key: key, Value: &apiv1.TopicEntry_MachineStats{MachineStats: message}}
		},
	}
}

func script(run func(context.Context, subscription.Sink[stats]) error) subscription.Source[stats] {
	return subscriptiontest.Script[stats](run)
}

func wait(ctx context.Context) error {
	<-ctx.Done()
	return ctx.Err()
}

// fail runs a source to its end and returns the failure its subscriber saw.
func fail(t *testing.T, source subscription.Source[stats]) *apiv1.TopicFailure {
	t.Helper()
	stream := subscriptiontest.Open(subscription.New(clock.System{}, topic(source)), 1, request)
	synctest.Wait()
	if err, ended := stream.Ended(); !ended || err != nil {
		t.Fatalf("stream ended=%t err=%v", ended, err)
	}
	frames := stream.Frames()
	last := frames[len(frames)-1]
	if err := protovalidate.Validate(last); err != nil {
		t.Fatal(err)
	}
	stream.Stop()
	return last.GetFailed()
}

func TestEveryWayASourceCanEndIsATypedFailure(t *testing.T) {
	cases := map[string]struct {
		run  func(context.Context, subscription.Sink[stats]) error
		code apiv1.TopicFailureCode
	}{
		"error": {
			func(context.Context, subscription.Sink[stats]) error { return errors.New("broken") },
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		},
		"return without error": {
			func(context.Context, subscription.Sink[stats]) error { return nil },
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		},
		"panic": {
			func(context.Context, subscription.Sink[stats]) error { panic("broken") },
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL,
		},
		"exits its goroutine without returning": {
			func(context.Context, subscription.Sink[stats]) error { runtime.Goexit(); return nil },
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		},
		"unsupported host": {
			func(context.Context, subscription.Sink[stats]) error { return subscription.Unsupported("no sampler") },
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED,
		},
		"too many keys": {
			func(ctx context.Context, sink subscription.Sink[stats]) error {
				for _, key := range []string{"a", "b", "c"} {
					sink.Put(key, value(1))
				}
				return wait(ctx)
			},
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
		},
		"oversized value": {
			func(ctx context.Context, sink subscription.Sink[stats]) error {
				huge := value(1)
				huge.Gpu = &apiv1.MachineStats_Gpus{Gpus: &apiv1.GpuList{}}
				for index := range 200 {
					huge.GetGpus().Gpus = append(huge.GetGpus().Gpus, &apiv1.GpuStats{
						Index: uint32(index), Name: strings.Repeat("g", 200), MemoryTotalBytes: 1,
					})
				}
				sink.Put("machine", huge)
				return wait(ctx)
			},
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
		},
		"value breaks its schema": {
			func(ctx context.Context, sink subscription.Sink[stats]) error {
				sink.Put("machine", &apiv1.MachineStats{})
				return wait(ctx)
			},
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		},
		"key breaks its schema": {
			func(ctx context.Context, sink subscription.Sink[stats]) error {
				sink.Put("", value(1))
				return wait(ctx)
			},
			apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		},
	}
	for name, each := range cases {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				if got := fail(t, script(each.run)); got.GetCode() != each.code {
					t.Fatalf("want %s, got %v", each.code, got)
				}
			})
		})
	}
}

// failsOverTheWire serves a source that fails with text the way gRPC sends it, and
// says whether the one frame it got is a failure that marshals and passes the
// schema. A detail is a source's own error text, a panic value or a tool's
// output, so it can be any bytes, and a frame that does not marshal ends the
// stream with a transport error that a client reading the code never sees.
func failsOverTheWire(t *testing.T, text string) bool {
	var typed atomic.Bool
	synctest.Test(t, func(t *testing.T) {
		hub := subscription.New(clock.System{}, topic(script(func(context.Context, subscription.Sink[stats]) error {
			return errors.New(text)
		})))
		frames, transport := serveAsTransport(hub)
		typed.Store(transport == nil && len(frames) == 1 && frames[0].GetFailed() != nil && protovalidate.Validate(frames[0]) == nil)
	})
	return typed.Load()
}

func TestAnyFailureDetailBecomesAFrameThatMarshals(t *testing.T) {
	property := func(detail []byte, repeat uint8) bool {
		return failsOverTheWire(t, strings.Repeat(string(detail), int(repeat)+1))
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 40}); err != nil {
		t.Fatal(err)
	}
	// The reported inputs: a byte no UTF-8 string holds, a detail over the schema's
	// bound, and both together.
	for name, text := range map[string]string{
		"invalid UTF-8":          "exec /usr/bin/tool: \xff",
		"over the bound":         strings.Repeat("é", 8192),
		"invalid and over bound": strings.Repeat("\xff", 5000),
		"cut inside a rune":      strings.Repeat("a", 4095) + "é",
	} {
		t.Run(name, func(t *testing.T) {
			if !failsOverTheWire(t, text) {
				t.Fatalf("a detail of %d bytes did not become a typed failure frame that marshals", len(text))
			}
		})
	}
}

// serveAsTransport serves one subscription the way gRPC sends it: every frame
// must marshal, and one that does not ends the stream with a transport error. It
// reports what the client saw once the hub and the source have settled.
func serveAsTransport(hub *subscription.Hub) (frames []*apiv1.SubscribeResponse, transport error) {
	ctx, cancel := context.WithCancel(context.Background())
	var mu sync.Mutex
	done := make(chan error, 1)
	go func() {
		done <- hub.Serve(ctx, 2, request, func(frame *apiv1.SubscribeResponse) error {
			if _, err := proto.Marshal(frame); err != nil {
				return err
			}
			mu.Lock()
			defer mu.Unlock()
			frames = append(frames, frame)
			return nil
		})
	}()
	synctest.Wait()
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		transport = err
	}
	mu.Lock()
	defer mu.Unlock()
	return frames, transport
}

// badText puts a value that satisfies the schema's rules and still cannot be
// marshalled: invalid UTF-8 in a key or in a string inside the value. The rules
// cannot see it, so the hub must, once, where every topic gets it.
func badText(text string, inKey bool) subscription.Source[stats] {
	return script(func(ctx context.Context, sink subscription.Sink[stats]) error {
		key, message := "machine", value(1)
		if inKey {
			key = text
		} else {
			message.Gpu = &apiv1.MachineStats_Gpus{Gpus: &apiv1.GpuList{Gpus: []*apiv1.GpuStats{{Name: text, MemoryTotalBytes: 1}}}}
		}
		sink.Put(key, message)
		sink.Synced()
		return wait(ctx)
	})
}

func TestAValueThatCannotBeMarshalledIsATypedFailureAndNeverRetained(t *testing.T) {
	property := func(raw []byte, inKey bool) bool {
		var clean atomic.Bool
		synctest.Test(t, func(t *testing.T) {
			hub := subscription.New(clock.System{}, topic(badText(string(raw), inKey)))
			// A subscriber that stays keeps the instance, and so any retained value.
			resident := subscriptiontest.Open(hub, 1, request)
			defer resident.Stop()
			synctest.Wait()
			_, transport := serveAsTransport(hub)
			clean.Store(transport == nil)
		})
		return clean.Load()
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 100}); err != nil {
		t.Fatal(err)
	}
	// The reported input: a name the schema's rules accept and no wire carries.
	for _, inKey := range []bool{false, true} {
		synctest.Test(t, func(t *testing.T) {
			hub := subscription.New(clock.System{}, topic(badText("\xff", inKey)))
			frames, transport := serveAsTransport(hub)
			if transport != nil || len(frames) != 1 || frames[0].GetFailed().GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED {
				t.Fatalf("inKey=%t: transport error %v, frames %v", inKey, transport, frames)
			}
		})
	}
}

// A source picks its own failure code, and the frame must still validate: a code
// the schema does not define would reach the client as a transport error.
func TestAnyFailureCodeBecomesAValidFrame(t *testing.T) {
	property := func(code int32) bool {
		var valid atomic.Bool
		synctest.Test(t, func(t *testing.T) {
			failed := fail(t, script(func(context.Context, subscription.Sink[stats]) error {
				return &subscription.Failure{Code: apiv1.TopicFailureCode(code), Detail: "x"}
			}))
			_, defined := apiv1.TopicFailureCode_name[code]
			chosen := defined && code != 0
			valid.Store(failed != nil && (!chosen || failed.GetCode() == apiv1.TopicFailureCode(code)))
		})
		return valid.Load()
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 100}); err != nil {
		t.Fatal(err)
	}
	for _, code := range []int32{0, 99, -1} {
		synctest.Test(t, func(t *testing.T) {
			failed := fail(t, script(func(context.Context, subscription.Sink[stats]) error {
				return &subscription.Failure{Code: apiv1.TopicFailureCode(code), Detail: "x"}
			}))
			if failed.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL {
				t.Fatalf("code %d gave %v", code, failed)
			}
		})
	}
}

func TestARequestWithoutAProducerIsRefusedAsUnsupported(t *testing.T) {
	for name, each := range map[string]*apiv1.SubscribeRequest{"no topic": {}, "no producer": request} {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				stream := subscriptiontest.Open(subscription.New(clock.System{}), 1, each)
				defer stream.Stop()
				synctest.Wait()
				frames := stream.Frames()
				if len(frames) != 1 || frames[0].GetFailed().GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED {
					t.Fatalf("frames %v", frames)
				}
			})
		})
	}
}

// withUnknown is the request with an unknown field, on the request itself or
// inside its topic message, as a client built from another schema would send.
func withUnknown(number protowire.Number, inTopic bool) *apiv1.SubscribeRequest {
	cloned := proto.Clone(request).(*apiv1.SubscribeRequest)
	raw := protowire.AppendVarint(protowire.AppendTag(nil, number, protowire.VarintType), 1)
	if inTopic {
		cloned.GetMachineStats().ProtoReflect().SetUnknown(raw)
	} else {
		cloned.ProtoReflect().SetUnknown(raw)
	}
	return cloned
}

// An unknown routing key is refused, not folded into the request's identity: a
// key built from the marshalled request would give each variant its own producer.
func TestARequestWithAnUnknownFieldIsRefusedAndStartsNoProducer(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscriptiontest.Count(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			sink.Put("machine", value(1))
			sink.Synced()
			return wait(ctx)
		}))
		hub := subscription.New(clock.System{}, topic(source))
		var streams []*subscriptiontest.Stream
		for index, inTopic := range []bool{false, true, false, true, false} {
			streams = append(streams, subscriptiontest.Open(hub, uint64(index), withUnknown(protowire.Number(1000+index), inTopic)))
		}
		synctest.Wait()
		for _, stream := range streams {
			frames := stream.Frames()
			if len(frames) != 1 || frames[0].GetFailed().GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED ||
				!strings.Contains(frames[0].GetFailed().GetDetail(), "unknown field") {
				t.Fatalf("frames %v", frames)
			}
			stream.Stop()
		}
		if starts := source.Starts.Load(); starts != 0 {
			t.Fatalf("%d producers started for requests no client of this schema sends", starts)
		}
	})
}

// idle is a source that holds its instance open and says nothing.
func idle() subscription.Source[stats] {
	return script(func(ctx context.Context, _ subscription.Sink[stats]) error { return wait(ctx) })
}

// refusedAsExhausted says whether a stream got one frame, a RESOURCE_EXHAUSTED
// failure, and ended.
func refusedAsExhausted(stream *subscriptiontest.Stream) bool {
	frames := stream.Frames()
	_, ended := stream.Ended()
	return ended && len(frames) == 1 && frames[0].GetFailed().GetCode() == apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED
}

// The limits are written as numbers, not as the constants that name them: a test
// that follows its constant passes whatever the constant is changed to.

func TestTheConnectionCapRefusesTheSixtyFifthAndOnlyThatConnection(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		hub := subscription.New(clock.System{}, topic(idle()))
		var streams []*subscriptiontest.Stream
		for range 64 {
			streams = append(streams, subscriptiontest.Open(hub, 1, request))
		}
		synctest.Wait()
		for number, stream := range streams {
			if _, ended := stream.Ended(); ended {
				t.Fatalf("subscription %d of 64 on one connection was refused: %v", number+1, stream.Frames())
			}
		}
		refused := subscriptiontest.Open(hub, 1, request)
		defer refused.Stop()
		synctest.Wait()
		if !refusedAsExhausted(refused) {
			t.Fatalf("the 65th subscription on a connection was not refused: %v", refused.Frames())
		}
		other := subscriptiontest.Open(hub, 2, request)
		synctest.Wait()
		if _, ended := other.Ended(); ended {
			t.Fatalf("second connection was refused: %v", other.Frames())
		}
		other.Stop()
		for _, stream := range streams {
			stream.Stop()
		}
	})
}

// The gateway cap is the sum over connections, so it is reached by sixteen
// connections at their own cap, and the next connection, with nothing of its own,
// is the one refused.
func TestTheGatewayCapRefusesTheThousandAndTwentyFifthSubscription(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		hub := subscription.New(clock.System{}, topic(idle()))
		var streams []*subscriptiontest.Stream
		for connection := uint64(1); connection <= 16; connection++ {
			for range 64 {
				streams = append(streams, subscriptiontest.Open(hub, connection, request))
			}
		}
		synctest.Wait()
		for _, stream := range streams {
			if _, ended := stream.Ended(); ended {
				t.Fatalf("a subscription within 1024 was refused: %v", stream.Frames())
			}
		}
		refused := subscriptiontest.Open(hub, 17, request)
		defer refused.Stop()
		synctest.Wait()
		if !refusedAsExhausted(refused) {
			t.Fatalf("the 1025th subscription on the gateway was not refused: %v", refused.Frames())
		}
		streams[0].Stop()
		again := subscriptiontest.Open(hub, 17, request)
		defer again.Stop()
		synctest.Wait()
		if _, ended := again.Ended(); ended {
			t.Fatalf("a freed slot was not reusable: %v", again.Frames())
		}
		for _, stream := range streams[1:] {
			stream.Stop()
		}
	})
}

// A client counts a stream quiet for 30 s as lost, so every healthy stream must be
// heard from at least every five seconds whatever its topic does. Six beats fit a
// lease, and the first must not wait for a second interval.
func TestAQuietStreamIsHeardFromEveryFiveSeconds(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		stream := subscriptiontest.Open(subscription.New(clock.System{}, topic(idle())), 1, request)
		defer stream.Stop()
		for beat := 1; beat <= 6; beat++ {
			time.Sleep(5 * time.Second)
			synctest.Wait()
			heartbeats := 0
			for _, frame := range stream.Frames() {
				if frame.GetHeartbeat() != nil {
					heartbeats++
				}
			}
			if heartbeats < beat {
				t.Fatalf("%d heartbeats after %d s of silence, want at least %d", heartbeats, 5*beat, beat)
			}
		}
	})
}

// A client that stops reading blocks only its own sender. The producer runs on,
// and the reader's next frame is the newest value, not a queue of old ones.
func TestABlockedClientHoldsUpNothingAndResumesAtTheNewestValue(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		const written = 1000
		release, proceed := make(chan struct{}), make(chan struct{})
		hub := subscription.New(clock.System{}, topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			sink.Put("machine", value(1))
			sink.Synced()
			<-proceed
			for second := range int64(written) {
				sink.Put("machine", value(2+second))
			}
			close(release)
			return wait(ctx)
		})))
		ctx, cancel := context.WithCancel(context.Background())
		var entries []int64
		done := make(chan error, 1)
		go func() {
			done <- hub.Serve(ctx, 1, request, func(frame *apiv1.SubscribeResponse) error {
				if entry := frame.GetEntry(); entry != nil {
					entries = append(entries, entry.GetMachineStats().SampledAt.Seconds)
					if len(entries) == 1 {
						close(proceed)
						<-release
					}
				}
				return nil
			})
		}()
		synctest.Wait()
		cancel()
		<-done
		if len(entries) != 2 || entries[1] != written+1 {
			t.Fatalf("entries %v", entries)
		}
	})
}

// The contract relies on the drop: a client reads a topic whose value never
// changes as stale, so a sampler that returns the same value for ever must look
// stale and not healthy. Each Put is settled before the next, so that dropping the
// equal value is what leaves the count at one, not the conflation of values that
// no subscriber has been sent yet.
func TestAnEqualValueSendsNothing(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		puts := make(chan stats)
		hub := subscription.New(clock.System{}, topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			for {
				select {
				case <-ctx.Done():
					return ctx.Err()
				case next := <-puts:
					sink.Put("machine", next)
				}
			}
		})))
		stream := subscriptiontest.Open(hub, 1, request)
		defer stream.Stop()
		entries := func() (count int) {
			for _, frame := range stream.Frames() {
				if frame.GetEntry() != nil {
					count++
				}
			}
			return count
		}
		for step, each := range []struct {
			put  stats
			want int
		}{
			{value(1), 1},
			{value(1), 1},
			{value(1), 1},
			{value(2), 2},
			{value(2), 2},
			{value(1), 3},
		} {
			puts <- each.put
			synctest.Wait()
			if got := entries(); got != each.want {
				t.Fatalf("after put %d (second %d) the subscriber has been sent %d entries, want %d", step, each.put.SampledAt.Seconds, got, each.want)
			}
		}
	})
}

func TestMinimumIntervalConflatesEachStream(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		limited := policy
		limited.MinInterval = 200 * time.Millisecond
		source := topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			for second := int64(1); ; second++ {
				sink.Put("machine", value(second))
				sink.Synced()
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(10 * time.Millisecond):
				}
			}
		}))
		source.Policy = limited
		stream := subscriptiontest.Open(subscription.New(clock.System{}, source), 1, request)
		defer stream.Stop()
		time.Sleep(time.Second)
		synctest.Wait()
		entries := 0
		for _, frame := range stream.Frames() {
			if frame.GetEntry() != nil {
				entries++
			}
		}
		if entries > 6 {
			t.Fatalf("%d entries in one second at a 200 ms minimum", entries)
		}
	})
}

// The floor spaces the changes after a snapshot; the snapshot itself is never held back,
// however many keys it has and however the source splits its Puts from its Synced.
func TestTheSnapshotIsNotHeldByTheMinimumInterval(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		limited := policy
		limited.MinInterval = 200 * time.Millisecond
		limited.MaxKeys = 8
		source := topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			for _, key := range []string{"a", "b", "c"} {
				sink.Put(key, value(1))
				runtime.Gosched()
			}
			sink.Synced()
			<-ctx.Done()
			return ctx.Err()
		}))
		source.Policy = limited
		stream := subscriptiontest.Open(subscription.New(clock.System{}, source), 1, request)
		defer stream.Stop()
		synctest.Wait()
		entries, synced, _, _ := subscriptiontest.Arms(stream.Received())
		if entries != 3 || synced != 1 {
			t.Fatalf("%d entries and %d synced frames with no time passed, want 3 and 1", entries, synced)
		}
	})
}

// A change right behind a flush waits out the floor, and one after a quiet spell does not.
func TestAQuietStreamIsAnsweredAtOnceAndABusyOneNoFasterThanTheFloor(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		limited := policy
		limited.MinInterval = 200 * time.Millisecond
		changes := make(chan int64)
		source := topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			sink.Put("machine", value(1))
			sink.Synced()
			for {
				select {
				case <-ctx.Done():
					return ctx.Err()
				case second := <-changes:
					sink.Put("machine", value(second))
				}
			}
		}))
		source.Policy = limited
		stream := subscriptiontest.Open(subscription.New(clock.System{}, source), 1, request)
		defer stream.Stop()
		synctest.Wait()
		if entries, _, _, _ := subscriptiontest.Arms(stream.Received()); entries != 1 {
			t.Fatalf("%d entries before any change", entries)
		}
		changes <- 2
		synctest.Wait()
		if entries, _, _, _ := subscriptiontest.Arms(stream.Received()); entries != 1 {
			t.Fatalf("a change inside the floor was sent at once: %d entries", entries)
		}
		time.Sleep(limited.MinInterval)
		synctest.Wait()
		if entries, _, _, _ := subscriptiontest.Arms(stream.Received()); entries != 2 {
			t.Fatalf("the change was not sent once the floor passed: %d entries", entries)
		}
		time.Sleep(10 * limited.MinInterval)
		changes <- 3
		synctest.Wait()
		if entries, _, _, _ := subscriptiontest.Arms(stream.Received()); entries != 3 {
			t.Fatalf("a change after a quiet spell was held: %d entries", entries)
		}
	})
}

// hot is a source that writes its one key every period, forever.
func hot(period time.Duration) subscription.Source[stats] {
	return script(func(ctx context.Context, sink subscription.Sink[stats]) error {
		for second := int64(1); ; second++ {
			sink.Put("machine", value(second))
			sink.Synced()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(period):
			}
		}
	})
}

// serveSlowly serves one subscription to a client that takes pause to read each
// frame, and counts what it was sent until the returned stop is called.
func serveSlowly(hub *subscription.Hub, pause time.Duration) (sent func() (entries, heartbeats int), stop func()) {
	ctx, cancel := context.WithCancel(context.Background())
	var entries, heartbeats atomic.Int64
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = hub.Serve(ctx, 1, request, func(frame *apiv1.SubscribeResponse) error {
			time.Sleep(pause)
			switch {
			case frame.GetEntry() != nil:
				entries.Add(1)
			case frame.GetHeartbeat() != nil:
				heartbeats.Add(1)
			}
			return nil
		})
	}()
	return func() (int, int) { return int(entries.Load()), int(heartbeats.Load()) }, func() {
		cancel()
		<-done
	}
}

// The interval exists for the client that cannot keep up, and a hot key refills
// as fast as a slow client reads it. A flush must end when it has sent what was
// owed at its start, or the interval never applies to the stream that needs it.
func TestMinimumIntervalHoldsForAClientSlowerThanTheProducer(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		limited := policy
		limited.MinInterval = 200 * time.Millisecond
		source := topic(hot(10 * time.Millisecond))
		source.Policy = limited
		sent, stop := serveSlowly(subscription.New(clock.System{}, source), 20*time.Millisecond)
		time.Sleep(time.Second)
		synctest.Wait()
		stop()
		if entries, _ := sent(); entries > 6 || entries < 4 {
			t.Fatalf("%d entries in one second at a 200 ms minimum, want 4 to 6", entries)
		}
	})
}

// A client kept busy by a hot key must still be sent its heartbeats: the longest
// gap between frames is the stream's only sign of life.
func TestHeartbeatsContinueWhileAHotKeyKeepsASlowClientBusy(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sent, stop := serveSlowly(subscription.New(clock.System{}, topic(hot(time.Millisecond))), 20*time.Millisecond)
		time.Sleep(time.Minute)
		synctest.Wait()
		stop()
		if _, heartbeats := sent(); heartbeats < int(time.Minute/subscription.HeartbeatInterval)-2 {
			t.Fatalf("%d heartbeats in a minute", heartbeats)
		}
	})
}

// A stopped source may take time to unwind, and a new subscriber can arrive
// meanwhile. Two producers of one instance must never overlap, however many
// subscribers come and go during the gap.
func TestProducersOfAnInstanceNeverOverlap(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var running, overlap atomic.Int64
		source := script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			if running.Add(1) > 1 {
				overlap.Add(1)
			}
			defer running.Add(-1)
			sink.Put("machine", value(1))
			sink.Synced()
			<-ctx.Done()
			time.Sleep(time.Second)
			return ctx.Err()
		})
		hub := subscription.New(clock.System{}, topic(source))
		for range 3 {
			stream := subscriptiontest.Open(hub, 1, request)
			synctest.Wait()
			stream.Stop()
		}
		stream := subscriptiontest.Open(hub, 1, request)
		time.Sleep(10 * time.Second)
		synctest.Wait()
		stream.Stop()
		time.Sleep(time.Second)
		if overlap.Load() != 0 {
			t.Fatalf("%d producers overlapped", overlap.Load())
		}
		if got := stream.Frames(); len(got) == 0 || got[0].GetEntry() == nil {
			t.Fatalf("the last subscriber got no value: %v", got)
		}
	})
}

func TestSampledNeverOverlapsAndKeepsTheCadence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var running, overlap, samples atomic.Int64
		source := subscription.Sampled("machine", time.Second, 500*time.Millisecond, clock.System{},
			each(func(ctx context.Context) (stats, error) {
				if running.Add(1) > 1 {
					overlap.Add(1)
				}
				defer running.Add(-1)
				<-ctx.Done()
				return value(samples.Add(1)), nil
			}))
		stream := subscriptiontest.Open(subscription.New(clock.System{}, topic(source)), 1, request)
		defer stream.Stop()
		time.Sleep(10*time.Second + 600*time.Millisecond)
		synctest.Wait()
		if overlap.Load() != 0 || samples.Load() != 11 {
			t.Fatalf("%d overlaps, %d samples", overlap.Load(), samples.Load())
		}
	})
}

func TestSampledDeadlineBoundsASlowSample(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscription.Sampled("machine", time.Hour, 5*time.Second, clock.System{},
			each(func(ctx context.Context) (stats, error) {
				<-ctx.Done()
				return value(1), nil
			}))
		stream := subscriptiontest.Open(subscription.New(clock.System{}, topic(source)), 1, request)
		defer stream.Stop()
		time.Sleep(5*time.Second - time.Nanosecond)
		synctest.Wait()
		if frames := stream.Frames(); len(frames) != 0 && frames[0].GetEntry() != nil {
			t.Fatal("sample ended before its deadline")
		}
		time.Sleep(time.Nanosecond)
		synctest.Wait()
		if frames := stream.Frames(); len(frames) == 0 {
			t.Fatal("deadline did not end the sample")
		}
	})
}

// stuck is a source that serves one snapshot and then blocks in a call that
// ignores its context, as a source inside a stuck tool or a hung mount does. Once
// the call returns it behaves.
func stuck(release chan struct{}) subscriptiontest.Counted[stats] {
	return subscriptiontest.Count(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
		sink.Put("machine", value(1))
		sink.Synced()
		<-release
		return wait(ctx)
	}))
}

// failedWith is the failure a stream ended on, or nil while it is still open.
func failedWith(stream *subscriptiontest.Stream) *apiv1.TopicFailure {
	frames := stream.Frames()
	if len(frames) == 0 {
		return nil
	}
	return frames[len(frames)-1].GetFailed()
}

// A source that will not stop cannot be killed. It must cost the gateway one
// goroutine however often clients come back, and every subscriber must hear a
// typed failure, never a stream of heartbeats.
func TestAProducerThatWillNotStopIsAccountedOnceAndTheTopicFailsTyped(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		release := make(chan struct{})
		source := stuck(release)
		hub := subscription.New(clock.System{}, topic(source))
		first := subscriptiontest.Open(hub, 1, request)
		synctest.Wait()
		first.Stop()
		goroutines := runtime.NumGoroutine()
		const retries = 200
		for range retries {
			retry := subscriptiontest.Open(hub, 1, request)
			time.Sleep(time.Second)
			synctest.Wait()
			retry.Stop()
		}
		if leaked := runtime.NumGoroutine() - goroutines; leaked > 2 {
			t.Fatalf("%d goroutines leaked over %d retries", leaked, retries)
		}
		time.Sleep(time.Minute)
		last := subscriptiontest.Open(hub, 1, request)
		defer last.Stop()
		time.Sleep(time.Hour)
		synctest.Wait()
		if failed := failedWith(last); failed.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED {
			t.Fatalf("a subscriber to a wedged topic got %v", last.Frames())
		}
		if err, ended := last.Ended(); !ended || err != nil {
			t.Fatalf("stream ended=%t err=%v after its failed frame", ended, err)
		}
		if starts := source.Starts.Load(); starts != 1 {
			t.Fatalf("%d producers started while the first would not stop", starts)
		}
		close(release)
		synctest.Wait()
		revived := subscriptiontest.Open(hub, 1, request)
		defer revived.Stop()
		time.Sleep(time.Second)
		synctest.Wait()
		if frames := revived.Frames(); len(frames) == 0 || frames[0].GetEntry() == nil || failedWith(revived) != nil {
			t.Fatalf("the topic stayed down after its producer returned: %v", frames)
		}
	})
}

// A subscriber that arrives while the old producer is stopping waits for it, for
// a bounded time. It must not stay subscribed to a stream with no producer.
func TestASubscriberWaitingOnAStoppingProducerIsFailedNotLeftWaiting(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		release := make(chan struct{})
		hub := subscription.New(clock.System{}, topic(stuck(release)))
		first := subscriptiontest.Open(hub, 1, request)
		synctest.Wait()
		first.Stop()
		second := subscriptiontest.Open(hub, 2, request)
		defer second.Stop()
		time.Sleep(time.Minute)
		synctest.Wait()
		if failed := failedWith(second); failed == nil {
			t.Fatalf("a subscriber still waits on a producer that will not stop: %v", second.Frames())
		}
		close(release)
	})
}

// A source that returns late, as a section waiting out its tool does, is the
// ordinary case and must not fail anyone: the waiting subscriber is served by the
// producer that starts when the old one returns.
func TestASubscriberWaitingOnASlowStopGetsAFreshProducer(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscriptiontest.Count(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			sink.Put("machine", value(1))
			sink.Synced()
			<-ctx.Done()
			time.Sleep(2 * time.Second)
			return ctx.Err()
		}))
		hub := subscription.New(clock.System{}, topic(source))
		first := subscriptiontest.Open(hub, 1, request)
		synctest.Wait()
		first.Stop()
		second := subscriptiontest.Open(hub, 2, request)
		time.Sleep(10 * time.Second)
		synctest.Wait()
		if frames := second.Frames(); len(frames) == 0 || frames[0].GetEntry() == nil || failedWith(second) != nil {
			t.Fatalf("frames %v", frames)
		}
		if starts := source.Starts.Load(); starts != 2 {
			t.Fatalf("%d producers started", starts)
		}
		second.Stop()
		time.Sleep(10 * time.Second)
	})
}

// A sample that ignores its context is a typed failure for its subscribers and
// one goroutine for the gateway, however often the topic is subscribed again.
func TestASampleThatIgnoresItsContextFailsTheTopicOnceAndIsNeverRepeated(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		release := make(chan struct{})
		var calls atomic.Int64
		source := subscription.Sampled("machine", time.Second, time.Second, clock.System{},
			each(func(context.Context) (stats, error) {
				calls.Add(1)
				<-release
				return value(1), nil
			}))
		hub := subscription.New(clock.System{}, topic(source))
		first := subscriptiontest.Open(hub, 1, request)
		defer first.Stop()
		time.Sleep(time.Minute)
		synctest.Wait()
		if failed := failedWith(first); failed.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED {
			t.Fatalf("a stuck sample gave %v", first.Frames())
		}
		goroutines := runtime.NumGoroutine()
		for range 50 {
			retry := subscriptiontest.Open(hub, 1, request)
			time.Sleep(time.Second)
			synctest.Wait()
			if failedWith(retry) == nil {
				t.Fatalf("a retry was served while a sample is stuck: %v", retry.Frames())
			}
			retry.Stop()
		}
		if leaked := runtime.NumGoroutine() - goroutines; leaked > 2 || calls.Load() != 1 {
			t.Fatalf("%d goroutines leaked, %d samples started", leaked, calls.Load())
		}
		close(release)
		synctest.Wait()
		revived := subscriptiontest.Open(hub, 1, request)
		defer revived.Stop()
		time.Sleep(time.Second)
		synctest.Wait()
		if frames := revived.Frames(); len(frames) == 0 || frames[0].GetEntry() == nil {
			t.Fatalf("the topic stayed down after its sample returned: %v", frames)
		}
	})
}

func TestAPanickingSampleIsATypedFailureNotACrash(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscription.Sampled("machine", time.Second, time.Second, clock.System{},
			each(func(context.Context) (stats, error) { panic("broken") }))
		if got := fail(t, source); got.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL {
			t.Fatalf("failure %v", got)
		}
	})
}

func TestSampledErrorEndsTheTopic(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscription.Sampled("machine", time.Second, time.Second, clock.System{},
			each(func(context.Context) (stats, error) { return nil, subscription.Unsupported("nope") }))
		if got := fail(t, source); got.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED {
			t.Fatalf("failure %v", got)
		}
	})
}

func TestDuplicateTopicIsAWiringDefect(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("a duplicate topic was accepted")
		}
	}()
	subscription.New(clock.System{}, topic(nil), topic(nil))
}
