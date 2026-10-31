package subscription_test

import (
	"context"
	"errors"
	"strings"
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

func TestFakeTopicPassesTheContract(t *testing.T) {
	subscriptiontest.Contract(t, func() subscriptiontest.Subject[stats] {
		fake := topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			sink.Put("machine", value(1))
			sink.Synced()
			return wait(ctx)
		}))
		fake.Policy = hostoperations.Topics[fake.Name]
		return subscriptiontest.Subject[stats]{
			Topic:   fake,
			Request: request,
			Failing: script(func(context.Context, subscription.Sink[stats]) error { return errors.New("broken") }),
			Failure: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		}
	})
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

func TestAnyFailureDetailBecomesAValidFrame(t *testing.T) {
	property := func(detail []byte, repeat uint8) bool {
		text := strings.Repeat(string(detail), int(repeat)+1)
		var valid atomic.Bool
		synctest.Test(t, func(t *testing.T) {
			failed := fail(t, script(func(context.Context, subscription.Sink[stats]) error { return errors.New(text) }))
			valid.Store(failed != nil)
		})
		return valid.Load()
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 40}); err != nil {
		t.Fatal(err)
	}
	synctest.Test(t, func(t *testing.T) {
		long := strings.Repeat("é", 8192)
		failed := fail(t, script(func(context.Context, subscription.Sink[stats]) error { return errors.New(long) }))
		if failed == nil {
			t.Fatal("a detail over the schema bound produced no failure frame")
		}
	})
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

func TestTheConnectionCapRefusesOnlyThatConnection(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		hub := subscription.New(clock.System{}, topic(script(func(ctx context.Context, _ subscription.Sink[stats]) error { return wait(ctx) })))
		var streams []*subscriptiontest.Stream
		for range subscription.MaxPerConnection {
			streams = append(streams, subscriptiontest.Open(hub, 1, request))
		}
		synctest.Wait()
		refused := subscriptiontest.Open(hub, 1, request)
		defer refused.Stop()
		synctest.Wait()
		frames := refused.Frames()
		if len(frames) != 1 || frames[0].GetFailed().GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED {
			t.Fatalf("frames %v", frames)
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

func TestAnEqualValueSendsNothing(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		stream := subscriptiontest.Open(subscription.New(clock.System{}, topic(script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			for range 5 {
				sink.Put("machine", value(1))
			}
			return wait(ctx)
		}))), 1, request)
		defer stream.Stop()
		synctest.Wait()
		if frames := stream.Frames(); len(frames) != 1 {
			t.Fatalf("frames %v", frames)
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
			func(ctx context.Context) (stats, error) {
				if running.Add(1) > 1 {
					overlap.Add(1)
				}
				defer running.Add(-1)
				<-ctx.Done()
				return value(samples.Add(1)), nil
			})
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
			func(ctx context.Context) (stats, error) {
				<-ctx.Done()
				return value(1), nil
			})
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

func TestSampledErrorEndsTheTopic(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		source := subscription.Sampled("machine", time.Second, time.Second, clock.System{},
			func(context.Context) (stats, error) { return nil, subscription.Unsupported("nope") })
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
