// Package subscriptiontest is the contract every topic passes. It runs a real
// Hub inside testing/synctest bubbles, so time is fake, a leaked goroutine fails
// the test, and a producer's clock, deadlines and tickers need no injection.
//
// The contract holds the topic, not only the hub: a sampled topic must refresh
// its value inside its stale bound, at no more than its cadence, without ever
// failing a healthy run, and must answer a stuck sampler with a typed absence or
// failure that costs the gateway nothing per retry. A topic whose value never
// changes between samples is wrong for the same reason: the hub drops an equal
// value, and the client then reads a healthy topic as stale.
package subscriptiontest

import (
	"context"
	"fmt"
	"runtime"
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
//
// Stuck is the topic's source on a machine where every call it can make never
// returns and ignores its context, with the function that lets the calls return.
// A sampled topic (one with a cadence) must supply it, because that is the
// failure its sample deadline exists for.
type Subject[V proto.Message] struct {
	Topic   subscription.Topic[V]
	Request *apiv1.SubscribeRequest
	Failing subscription.Source[V]
	Failure apiv1.TopicFailureCode
	Stuck   func() (source subscription.Source[V], release func())
}

// Received is one frame and the time it reached the client.
type Received struct {
	At    time.Time
	Frame *apiv1.SubscribeResponse
}

// Stream is one open subscription, recording every frame it is sent.
type Stream struct {
	cancel context.CancelFunc
	done   chan error

	mu       sync.Mutex
	received []Received
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
			stream.received = append(stream.received, Received{time.Now(), frame})
			return nil
		})
	}()
	return stream
}

// Received returns a copy of what the stream has been sent so far, with times.
func (s *Stream) Received() []Received {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.received)
}

// Frames returns a copy of what the stream has been sent so far.
func (s *Stream) Frames() []*apiv1.SubscribeResponse {
	var frames []*apiv1.SubscribeResponse
	for _, each := range s.Received() {
		frames = append(frames, each.Frame)
	}
	return frames
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

// Counted reports how often a source started, how many instances run now, and
// how many values it produced. A sampled topic puts one value per sample, so Puts
// is the load it put on its host.
type Counted[V proto.Message] struct {
	Source  subscription.Source[V]
	Starts  *atomic.Int64
	Running *atomic.Int64
	Puts    *atomic.Int64
}

// Count wraps a source.
func Count[V proto.Message](source subscription.Source[V]) Counted[V] {
	return Counted[V]{source, &atomic.Int64{}, &atomic.Int64{}, &atomic.Int64{}}
}

// Run counts one start of the wrapped source, and each value it puts.
func (c Counted[V]) Run(ctx context.Context, sink subscription.Sink[V]) error {
	c.Starts.Add(1)
	c.Running.Add(1)
	defer c.Running.Add(-1)
	return c.Source.Run(ctx, putCounter[V]{sink, c.Puts})
}

type putCounter[V proto.Message] struct {
	subscription.Sink[V]
	puts *atomic.Int64
}

func (c putCounter[V]) Put(key string, value V) {
	c.puts.Add(1)
	c.Sink.Put(key, value)
}

// Arms counts the frames of each kind on a stream.
func Arms(received []Received) (entries, synced, heartbeats, failed int) {
	for _, each := range received {
		switch each.Frame.Frame.(type) {
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

// entryTimes lists when entries arrived.
func entryTimes(received []Received) []time.Time {
	var times []time.Time
	for _, each := range received {
		if each.Frame.GetEntry() != nil {
			times = append(times, each.At)
		}
	}
	return times
}

func failureOf(received []Received) *apiv1.TopicFailure {
	for _, each := range received {
		if failed := each.Frame.GetFailed(); failed != nil {
			return failed
		}
	}
	return nil
}

func hub[V proto.Message](topic subscription.Topic[V], source subscription.Source[V]) *subscription.Hub {
	topic.Source = source
	return subscription.New(clock.System{}, topic)
}

func hung[V proto.Message](ctx context.Context, _ subscription.Sink[V]) error {
	<-ctx.Done()
	return ctx.Err()
}

func requireSnapshot(received []Received, name string) error {
	frames := make([]*apiv1.SubscribeResponse, len(received))
	for index, each := range received {
		frames[index] = each.Frame
	}
	synced := slices.IndexFunc(frames, func(frame *apiv1.SubscribeResponse) bool { return frame.GetSynced() != nil })
	if synced < 0 {
		return fmt.Errorf("no synced frame in %v", frames)
	}
	if entries, _, _, _ := Arms(received[:synced]); entries == 0 {
		return fmt.Errorf("synced before any entry: %v", frames)
	}
	for _, frame := range frames {
		entry := frame.GetEntry()
		if entry == nil {
			continue
		}
		message := entry.ProtoReflect()
		arm := message.WhichOneof(message.Descriptor().Oneofs().ByName("value"))
		if arm == nil || string(arm.Name()) != name {
			return fmt.Errorf("topic %s sent value arm %v", name, arm)
		}
	}
	return nil
}

// observation is how long a case watches a topic: many times its longest bound,
// so a topic that fails, stops or speeds up late is seen doing it. It derives
// from the policy's non-zero bounds, because an event-driven topic has none of
// the sampled ones.
func observation(policy hostoperations.TopicPolicy) time.Duration {
	return 10 * max(policy.FirstFrame, policy.StaleAfter, policy.Cadence, subscription.HeartbeatInterval)
}

// scenario is one case: it runs inside a bubble and returns what it found wrong.
type scenario[V proto.Message] struct {
	name string
	// sampled cases apply to a topic with a cadence.
	sampled bool
	check   func(Subject[V], hostoperations.TopicPolicy) error
}

// Violations runs every case against a fresh subject, each in its own bubble,
// and returns what each found wrong. build must make a new source for each call,
// because a bubble has its own time and a sampled source remembers when it last
// sampled. It is how the contract itself is tested: a wrong topic must have a
// violation in the case that names its defect.
func Violations[V proto.Message](t *testing.T, build func() Subject[V]) map[string]error {
	t.Helper()
	found := map[string]error{}
	for _, each := range scenarios[V]() {
		synctest.Test(t, func(t *testing.T) {
			found[each.name] = run(build(), each)
		})
	}
	return found
}

func run[V proto.Message](subject Subject[V], each scenario[V]) error {
	policy, generated := hostoperations.Topics[subject.Topic.Name]
	switch {
	case !generated:
		return fmt.Errorf("topic %s is not generated", subject.Topic.Name)
	case subject.Topic.Policy != policy:
		return fmt.Errorf("topic %s policy %+v, generated %+v", subject.Topic.Name, subject.Topic.Policy, policy)
	case each.sampled && policy.Cadence == 0:
		return nil
	case each.sampled && policy.StaleAfter <= policy.Cadence:
		return fmt.Errorf("a sampled topic is stale before its next sample: stale_after_ms %s, cadence_ms %s", policy.StaleAfter, policy.Cadence)
	case each.sampled && subject.Stuck == nil:
		return fmt.Errorf("topic %s is sampled and says nothing of its sampler getting stuck: supply Subject.Stuck", subject.Topic.Name)
	}
	return each.check(subject, policy)
}

// Contract runs every case against a fresh subject, built inside each bubble. For
// the same reason as Violations, build makes a new source each time.
func Contract[V proto.Message](t *testing.T, build func() Subject[V]) {
	t.Helper()
	for _, each := range scenarios[V]() {
		t.Run(each.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				if err := run(build(), each); err != nil {
					t.Fatal(err)
				}
			})
		})
	}
}

func scenarios[V proto.Message]() []scenario[V] {
	return []scenario[V]{
		{"snapshot then synced within the first-frame bound", false, snapshotInTime[V]},
		{"every frame validates and stays within the bounds", false, framesStayValid[V]},
		{"a second subscriber gets the retained snapshot from the same producer", false, snapshotIsShared[V]},
		{"the hub heartbeats under a silent producer", false, heartbeatsUnderSilence[V]},
		{"no frame after cancel", false, silentAfterCancel[V]},
		{"the last subscriber stops the producer and the next starts fresh", false, lastSubscriberStops[V]},
		{"a failing source ends every stream with its typed failure", false, failureIsTyped[V]},
		{"a healthy run never fails and never goes quiet", false, healthyRunStaysUp[V]},
		{"values refresh inside the stale bound and no faster than the cadence", true, valuesKeepTheCadence[V]},
		{"subscribing again and again samples no faster than the cadence", true, resubscribingKeepsTheCadence[V]},
		{"a stuck sampler is a typed absence or failure and costs nothing per retry", true, stuckSamplerIsBounded[V]},
	}
}

func snapshotInTime[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
	defer stream.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	return requireSnapshot(stream.Received(), subject.Topic.Name)
}

func framesStayValid[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
	defer stream.Stop()
	time.Sleep(observation(policy))
	synctest.Wait()
	keys := map[string]struct{}{}
	for _, frame := range stream.Frames() {
		if err := protovalidate.Validate(frame); err != nil {
			return fmt.Errorf("frame %v: %w", frame, err)
		}
		if entry := frame.GetEntry(); entry != nil {
			keys[entry.Key] = struct{}{}
			if size := proto.Size(entry); size > policy.MaxValueBytes+len(entry.Key)+16 {
				return fmt.Errorf("entry of %d bytes exceeds the topic's %d", size, policy.MaxValueBytes)
			}
		}
	}
	if len(keys) > policy.MaxKeys {
		return fmt.Errorf("%d keys exceed the topic's %d", len(keys), policy.MaxKeys)
	}
	return nil
}

func snapshotIsShared[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	source := Count(subject.Topic.Source)
	shared := hub(subject.Topic, subscription.Source[V](source))
	first := Open(shared, 1, subject.Request)
	defer first.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	second := Open(shared, 2, subject.Request)
	defer second.Stop()
	synctest.Wait()
	if err := requireSnapshot(second.Received(), subject.Topic.Name); err != nil {
		return err
	}
	if starts := source.Starts.Load(); starts != 1 {
		return fmt.Errorf("%d producers started", starts)
	}
	return nil
}

// heartbeatsUnderSilence holds the hub, not the topic: it swaps in a producer
// that says nothing. The topic's own liveness is held by the sampled cases.
func heartbeatsUnderSilence[V proto.Message](subject Subject[V], _ hostoperations.TopicPolicy) error {
	stream := Open(hub(subject.Topic, subscription.Source[V](Script[V](hung[V]))), 1, subject.Request)
	defer stream.Stop()
	const beats = 4
	time.Sleep(beats * subscription.HeartbeatInterval)
	synctest.Wait()
	if entries, synced, heartbeats, failed := Arms(stream.Received()); entries+synced+failed != 0 || heartbeats != beats {
		return fmt.Errorf("want %d heartbeats and nothing else, got %v", beats, stream.Frames())
	}
	return nil
}

func silentAfterCancel[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	stream.Stop()
	sent := len(stream.Frames())
	time.Sleep(observation(policy))
	synctest.Wait()
	if after := len(stream.Frames()); after != sent {
		return fmt.Errorf("%d frames after cancel", after-sent)
	}
	return nil
}

func lastSubscriberStops[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	source := Count(subject.Topic.Source)
	shared := hub(subject.Topic, subscription.Source[V](source))
	first, second := Open(shared, 1, subject.Request), Open(shared, 2, subject.Request)
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	first.Stop()
	synctest.Wait()
	if running := source.Running.Load(); running != 1 {
		second.Stop()
		return fmt.Errorf("%d producers run with a subscriber left", running)
	}
	second.Stop()
	synctest.Wait()
	if running := source.Running.Load(); running != 0 {
		return fmt.Errorf("%d producers run with no subscriber", running)
	}
	third := Open(shared, 3, subject.Request)
	defer third.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	if err := requireSnapshot(third.Received(), subject.Topic.Name); err != nil {
		return err
	}
	if starts := source.Starts.Load(); starts != 2 {
		return fmt.Errorf("%d producers started", starts)
	}
	return nil
}

func failureIsTyped[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	shared := hub(subject.Topic, subject.Failing)
	first, second := Open(shared, 1, subject.Request), Open(shared, 2, subject.Request)
	defer first.Stop()
	defer second.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	for _, stream := range []*Stream{first, second} {
		frames := stream.Frames()
		if len(frames) == 0 || frames[len(frames)-1].GetFailed().GetCode() != subject.Failure {
			return fmt.Errorf("want failure %s, got %v", subject.Failure, frames)
		}
		if err := protovalidate.Validate(frames[len(frames)-1]); err != nil {
			return err
		}
		if err, ended := stream.Ended(); !ended || err != nil {
			return fmt.Errorf("stream ended=%t err=%v after its failed frame", ended, err)
		}
	}
	return nil
}

// healthyRunStaysUp watches a working topic for many of its longest bounds. A
// topic that fails late, or ends its stream, or leaves a gap longer than the
// heartbeat, is wrong whatever its first seconds look like.
func healthyRunStaysUp[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	start := time.Now()
	stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
	defer stream.Stop()
	time.Sleep(observation(policy))
	synctest.Wait()
	received := stream.Received()
	if failed := failureOf(received); failed != nil {
		return fmt.Errorf("a healthy topic failed with %v", failed)
	}
	if err, ended := stream.Ended(); ended {
		return fmt.Errorf("a healthy topic's stream ended: %v", err)
	}
	last := start
	for _, each := range received {
		if gap := each.At.Sub(last); gap > subscription.HeartbeatInterval {
			return fmt.Errorf("%s with no frame, past the %s heartbeat", gap, subscription.HeartbeatInterval)
		}
		last = each.At
	}
	if gap := start.Add(observation(policy)).Sub(last); gap > subscription.HeartbeatInterval {
		return fmt.Errorf("%s with no frame at the end of the run", gap)
	}
	return nil
}

// valuesKeepTheCadence holds the two bounds a client relies on. A value must
// arrive inside stale_after_ms, or a working topic reads as stale. And values
// must not outrun cadence_ms, or the topic costs its host more than designed:
// the cadence is the whole rate limit on a shared login node.
func valuesKeepTheCadence[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	start := time.Now()
	stream := Open(hub(subject.Topic, subject.Topic.Source), 1, subject.Request)
	defer stream.Stop()
	window := observation(policy)
	time.Sleep(window)
	synctest.Wait()
	times := entryTimes(stream.Received())
	if len(times) == 0 {
		return fmt.Errorf("no value in %s", window)
	}
	if first := times[0].Sub(start); first > policy.FirstFrame {
		return fmt.Errorf("first value after %s, past the %s first-frame bound", first, policy.FirstFrame)
	}
	last := start
	for _, at := range times {
		if gap := at.Sub(last); gap > policy.StaleAfter {
			return fmt.Errorf("%s between values, past the %s stale bound", gap, policy.StaleAfter)
		}
		last = at
	}
	if gap := start.Add(window).Sub(last); gap > policy.StaleAfter {
		return fmt.Errorf("no value for the last %s, past the %s stale bound", gap, policy.StaleAfter)
	}
	if most := int(window/policy.Cadence) + 1; len(times) > most {
		return fmt.Errorf("%d values in %s at a %s cadence, at most %d", len(times), window, policy.Cadence, most)
	}
	return nil
}

// resubscribingKeepsTheCadence holds the same rate limit against a client that
// subscribes, leaves and subscribes again: a Machines page that remounts, or a
// retry loop. Each subscription starts a producer, and a producer that sampled at
// once would cost the host a sample per subscription however short, because the
// subscription's own wait ends with its producer. Each stay is a fifth of the
// cadence, long enough for a first sample on a working topic.
func resubscribingKeepsTheCadence[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	source := Count(subject.Topic.Source)
	shared := hub(subject.Topic, subscription.Source[V](source))
	const cycles = 100
	stay := policy.Cadence / 5
	for range cycles {
		stream := Open(shared, 1, subject.Request)
		time.Sleep(stay)
		synctest.Wait()
		stream.Stop()
		synctest.Wait()
	}
	window := cycles * stay
	if most := int(window/policy.Cadence) + 1; source.Puts.Load() > int64(most) {
		return fmt.Errorf("%d samples over %d subscriptions in %s at a %s cadence, at most %d",
			source.Puts.Load(), cycles, window, policy.Cadence, most)
	}
	last := Open(shared, 1, subject.Request)
	defer last.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	if err := requireSnapshot(last.Received(), subject.Topic.Name); err != nil {
		return fmt.Errorf("the topic stopped serving after %d subscriptions: %w", cycles, err)
	}
	return nil
}

// stuckSamplerIsBounded is the sample deadline's contract. Calls that never
// return must cost the topic a typed answer, and the gateway one resource at most.
// A typed answer is either values with the stuck section absent or a typed failure
// of the whole topic. A topic that fails whole where it could have reported one
// section absent passes here, because a generic contract cannot know its sections;
// the topic's own tests hold that.
//
// The goroutine count is the process's, which only this bubble changes while no
// gateway test runs in parallel.
func stuckSamplerIsBounded[V proto.Message](subject Subject[V], policy hostoperations.TopicPolicy) error {
	stuck, release := subject.Stuck()
	released := false
	defer func() {
		if !released {
			release()
		}
	}()
	shared := hub(subject.Topic, stuck)
	stream := Open(shared, 1, subject.Request)
	if err := answers(stream, policy); err != nil {
		stream.Stop()
		return err
	}
	stream.Stop()

	// Subscribing again and again must not stack work behind the stuck calls.
	goroutines := runtime.NumGoroutine()
	const retries = 20
	for retry := range retries {
		again := Open(shared, 1, subject.Request)
		if err := answers(again, policy); err != nil {
			again.Stop()
			return fmt.Errorf("retry %d: %w", retry, err)
		}
		again.Stop()
	}
	if leaked := runtime.NumGoroutine() - goroutines; leaked > 3 {
		return fmt.Errorf("%d goroutines leaked over %d subscriptions to a stuck sampler", leaked, retries)
	}

	release()
	released = true
	synctest.Wait()
	fresh := Open(shared, 1, subject.Request)
	defer fresh.Stop()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	if err := requireSnapshot(fresh.Received(), subject.Topic.Name); err != nil {
		return fmt.Errorf("the topic stayed down after its stuck calls returned: %w", err)
	}
	return nil
}

// answers requires a stream on a stuck topic to say something about it by the
// first-frame bound, and to keep saying it: a typed failure ends the stream, and
// otherwise values arrive inside the stale bound for the whole observation.
// Heartbeats alone are a stream that looks alive and tells nothing.
func answers(stream *Stream, policy hostoperations.TopicPolicy) error {
	start := time.Now()
	time.Sleep(policy.FirstFrame)
	synctest.Wait()
	received := stream.Received()
	if failed := failureOf(received); failed != nil {
		if err := protovalidate.Validate(received[len(received)-1].Frame); err != nil {
			return err
		}
		return nil
	}
	if len(entryTimes(received)) == 0 {
		return fmt.Errorf("only heartbeats %s into a stuck sampler, past the first-frame bound: %v", policy.FirstFrame, stream.Frames())
	}
	time.Sleep(observation(policy) - policy.FirstFrame)
	synctest.Wait()
	received = stream.Received()
	if failureOf(received) != nil {
		return nil
	}
	last := start
	for _, at := range entryTimes(received) {
		if gap := at.Sub(last); gap > policy.StaleAfter {
			return fmt.Errorf("a stuck sampler went %s between values, past the %s stale bound", gap, policy.StaleAfter)
		}
		last = at
	}
	if gap := start.Add(observation(policy)).Sub(last); gap > policy.StaleAfter {
		return fmt.Errorf("a stuck sampler sent nothing for the last %s, past the %s stale bound", gap, policy.StaleAfter)
	}
	return nil
}
