// Package subscriptiontest is the contract every topic passes. It runs a real
// Hub inside testing/synctest bubbles, so time is fake, a leaked goroutine fails
// the test, and a producer's clock, deadlines and tickers need no injection.
package subscriptiontest

import (
	"context"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"google.golang.org/protobuf/proto"
)

// Subject is one topic under contract. Topic.Source is its healthy producer.
// Failing is the same topic unable to work, and Failure the code it reports.
type Subject[V proto.Message] struct {
	Topic   subscription.Topic[V]
	Request *apiv1.SubscribeRequest
	Failing subscription.Source[V]
	Failure apiv1.TopicFailureCode
}

// Stream is one open subscription, recording every frame it is sent.
type Stream struct {
	cancel context.CancelFunc
	done   chan error

	mu     sync.Mutex
	frames []*apiv1.SubscribeResponse
}

// Open serves a subscription in a new goroutine. The caller must Stop it, or
// let a terminal frame end it, before its bubble ends.
func Open(hub *subscription.Hub, connection uint64, request *apiv1.SubscribeRequest) *Stream {
	ctx, cancel := context.WithCancel(context.Background())
	stream := &Stream{cancel: cancel, done: make(chan error, 1)}
	go func() {
		stream.done <- hub.Serve(ctx, connection, request, func(frame *apiv1.SubscribeResponse) error {
			stream.mu.Lock()
			defer stream.mu.Unlock()
			stream.frames = append(stream.frames, frame)
			return nil
		})
	}()
	return stream
}

// Frames returns a copy of what the stream has been sent so far.
func (s *Stream) Frames() []*apiv1.SubscribeResponse {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.frames)
}

// Stop cancels the subscription and waits for Serve to return.
func (s *Stream) Stop() {
	s.cancel()
	<-s.done
}

// Ended reports Serve's result if it has returned, for use after synctest.Wait.
func (s *Stream) Ended() (error, bool) {
	select {
	case err := <-s.done:
		s.done <- err
		return err, true
	default:
		return nil, false
	}
}

// Script is a Source written as a function, for scripted producers in tests.
type Script[V proto.Message] func(context.Context, subscription.Sink[V]) error

// Run calls the function.
func (s Script[V]) Run(ctx context.Context, sink subscription.Sink[V]) error { return s(ctx, sink) }

// Counted reports how often a source started and how many instances run now.
type Counted[V proto.Message] struct {
	Source  subscription.Source[V]
	Starts  *atomic.Int64
	Running *atomic.Int64
}

// Count wraps a source.
func Count[V proto.Message](source subscription.Source[V]) Counted[V] {
	return Counted[V]{source, &atomic.Int64{}, &atomic.Int64{}}
}

// Run counts one start of the wrapped source.
func (c Counted[V]) Run(ctx context.Context, sink subscription.Sink[V]) error {
	c.Starts.Add(1)
	c.Running.Add(1)
	defer c.Running.Add(-1)
	return c.Source.Run(ctx, sink)
}

// Count of frames of each arm on a stream.
func arms(frames []*apiv1.SubscribeResponse) (entries, synced, heartbeats, failed int) {
	for _, frame := range frames {
		switch frame.Frame.(type) {
		case *apiv1.SubscribeResponse_Entry:
			entries++
		case *apiv1.SubscribeResponse_Synced:
			synced++
		case *apiv1.SubscribeResponse_Heartbeat:
			heartbeats++
		case *apiv1.SubscribeResponse_Failed:
			failed++
		}
	}
	return entries, synced, heartbeats, failed
}

func hub[V proto.Message](topic subscription.Topic[V], source subscription.Source[V]) *subscription.Hub {
	topic.Source = source
	return subscription.New(clock.System{}, topic)
}

func hung[V proto.Message](ctx context.Context, _ subscription.Sink[V]) error {
	<-ctx.Done()
	return ctx.Err()
}

func requireSnapshot(t *testing.T, frames []*apiv1.SubscribeResponse, name string) {
	t.Helper()
	synced := slices.IndexFunc(frames, func(frame *apiv1.SubscribeResponse) bool { return frame.GetSynced() != nil })
	if synced < 0 {
		t.Fatalf("no synced frame in %v", frames)
	}
	entries, _, _, _ := arms(frames[:synced])
	if entries == 0 {
		t.Fatalf("synced before any entry: %v", frames)
	}
	for _, frame := range frames {
		entry := frame.GetEntry()
		if entry == nil {
			continue
		}
		message := entry.ProtoReflect()
		arm := message.WhichOneof(message.Descriptor().Oneofs().ByName("value"))
		if arm == nil || string(arm.Name()) != name {
			t.Fatalf("topic %s sent value arm %v", name, arm)
		}
	}
}

// Contract runs every case against a fresh subject, built inside each bubble.
func Contract[V proto.Message](t *testing.T, build func() Subject[V]) {
	t.Helper()
	bubble := func(name string, body func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy)) {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				subject := build()
				policy, generated := hostoperations.Topics[subject.Topic.Name]
				if !generated {
					t.Fatalf("topic %s is not generated", subject.Topic.Name)
				}
				if subject.Topic.Policy != policy {
					t.Fatalf("topic %s policy %+v, generated %+v", subject.Topic.Name, subject.Topic.Policy, policy)
				}
				body(t, subject, policy)
			})
		})
	}

	bubble("snapshot then synced within the first-frame bound", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
		defer stream.Stop()
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		requireSnapshot(t, stream.Frames(), subject.Topic.Name)
	})

	bubble("every frame validates and stays within the bounds", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
		defer stream.Stop()
		time.Sleep(2 * policy.StaleAfter)
		synctest.Wait()
		keys := map[string]struct{}{}
		for _, frame := range stream.Frames() {
			if err := protovalidate.Validate(frame); err != nil {
				t.Fatalf("frame %v: %v", frame, err)
			}
			if entry := frame.GetEntry(); entry != nil {
				keys[entry.Key] = struct{}{}
				if size := proto.Size(entry); size > policy.MaxValueBytes+len(entry.Key)+16 {
					t.Fatalf("entry of %d bytes exceeds the topic's %d", size, policy.MaxValueBytes)
				}
			}
		}
		if len(keys) > policy.MaxKeys {
			t.Fatalf("%d keys exceed the topic's %d", len(keys), policy.MaxKeys)
		}
	})

	bubble("a second subscriber gets the retained snapshot from the same producer", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		source := Count(subject.Topic.Source)
		shared := hub(subject.Topic, subscription.Source[V](source))
		first := Open(shared, 1, subject.Request)
		defer first.Stop()
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		second := Open(shared, 2, subject.Request)
		defer second.Stop()
		synctest.Wait()
		requireSnapshot(t, second.Frames(), subject.Topic.Name)
		if starts := source.Starts.Load(); starts != 1 {
			t.Fatalf("%d producers started", starts)
		}
	})

	bubble("heartbeats continue while the producer is silent or hung", func(t *testing.T, subject Subject[V], _ hostoperations.TopicPolicy) {
		stream := Open(hub(subject.Topic, subscription.Source[V](Script[V](hung[V]))), 1, subject.Request)
		defer stream.Stop()
		const beats = 4
		time.Sleep(beats * subscription.HeartbeatInterval)
		synctest.Wait()
		frames := stream.Frames()
		if entries, synced, heartbeats, failed := arms(frames); entries+synced+failed != 0 || heartbeats != beats {
			t.Fatalf("want %d heartbeats and nothing else, got %v", beats, frames)
		}
	})

	bubble("no frame after cancel", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		stream.Stop()
		sent := len(stream.Frames())
		time.Sleep(2 * policy.StaleAfter)
		synctest.Wait()
		if after := len(stream.Frames()); after != sent {
			t.Fatalf("%d frames after cancel", after-sent)
		}
	})

	bubble("the last subscriber stops the producer and the next starts fresh", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		source := Count(subject.Topic.Source)
		shared := hub(subject.Topic, subscription.Source[V](source))
		first, second := Open(shared, 1, subject.Request), Open(shared, 2, subject.Request)
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		first.Stop()
		synctest.Wait()
		if running := source.Running.Load(); running != 1 {
			t.Fatalf("%d producers run with a subscriber left", running)
		}
		second.Stop()
		synctest.Wait()
		if running := source.Running.Load(); running != 0 {
			t.Fatalf("%d producers run with no subscriber", running)
		}
		third := Open(shared, 3, subject.Request)
		defer third.Stop()
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		requireSnapshot(t, third.Frames(), subject.Topic.Name)
		if starts := source.Starts.Load(); starts != 2 {
			t.Fatalf("%d producers started", starts)
		}
	})

	bubble("a failing source ends every stream with its typed failure", func(t *testing.T, subject Subject[V], policy hostoperations.TopicPolicy) {
		shared := hub(subject.Topic, subject.Failing)
		first, second := Open(shared, 1, subject.Request), Open(shared, 2, subject.Request)
		defer first.Stop()
		defer second.Stop()
		time.Sleep(policy.FirstFrame)
		synctest.Wait()
		for _, stream := range []*Stream{first, second} {
			frames := stream.Frames()
			if len(frames) == 0 || frames[len(frames)-1].GetFailed().GetCode() != subject.Failure {
				t.Fatalf("want failure %s, got %v", subject.Failure, frames)
			}
			if err := protovalidate.Validate(frames[len(frames)-1]); err != nil {
				t.Fatal(err)
			}
			if err, ended := stream.Ended(); !ended || err != nil {
				t.Fatalf("stream ended=%t err=%v after its failed frame", ended, err)
			}
		}
	})
}
