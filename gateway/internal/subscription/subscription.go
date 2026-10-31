// Package subscription is the gateway's generic live-state primitive. A topic
// supplies facts through a Source; this package owns everything about their
// lifetime: one producer per topic instance, the latest-value map, conflation,
// snapshot then changes, heartbeats, caps, typed failures and last-subscriber stop.
//
// It fits a topic when a reader who missed five updates only wants the sixth.
// Delivery is latest value per key: values may be skipped, and each key's values
// stay in order. Anything needing every value belongs on Tail.
package subscription

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// HeartbeatInterval is the longest gap between frames on a healthy stream.
const HeartbeatInterval = 5 * time.Second

// StopGrace is how long a producer may take to return once it is cancelled. A
// source that ignores its context cannot be killed, so past this the hub stops
// waiting: it fails the subscribers that waited and refuses new ones with a typed
// failure, until the stuck source returns. It stays under every topic's first
// frame bound, so a subscriber hears the failure before it would give up.
const StopGrace = 5 * time.Second

const (
	// MaxPerConnection and MaxPerGateway cap open subscriptions.
	MaxPerConnection = 64
	MaxPerGateway    = 1024
	maxDetailBytes   = 4096
)

// Sink receives a producer's facts. It never blocks on a subscriber. Put takes
// ownership of value, so a source must not change it afterwards. Synced says the
// state so far is one complete snapshot; calling it again has no effect.
type Sink[V proto.Message] interface {
	Put(key string, value V)
	Remove(key string)
	Synced()
}

// Source produces one topic's facts. Run returns when ctx ends, within
// StopGrace. Any earlier return ends the stream for every subscriber: an error
// as SOURCE_FAILED, or its own code through Failure, and a panic as INTERNAL.
// A Run that never returns costs one goroutine and blocks its topic instance
// until it does: new subscribers get a typed failure, and nothing overlaps it.
type Source[V proto.Message] interface {
	Run(ctx context.Context, sink Sink[V]) error
}

// Failure is an error a source returns to choose its typed failure code.
type Failure struct {
	Code   apiv1.TopicFailureCode
	Detail string
}

func (f *Failure) Error() string { return fmt.Sprintf("%s: %s", f.Code, f.Detail) }

// Unsupported reports that this host cannot produce the topic. Retrying will not help.
func Unsupported(detail string) error {
	return &Failure{Code: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED, Detail: detail}
}

// Topic is one SubscribeRequest.topic arm. Name and Policy come from the
// generated table. Entry pairs a value with its TopicEntry arm, which is how the
// type system holds what the wire schema cannot.
type Topic[V proto.Message] struct {
	Name   string
	Policy hostoperations.TopicPolicy
	Source Source[V]
	Entry  func(key string, value V) *apiv1.TopicEntry
}

// Registration is a Topic of any value type, as the hub holds it.
type Registration interface {
	registration() registered
}

type registered struct {
	name   string
	policy hostoperations.TopicPolicy
	run    func(context.Context, producer) error
}

func (t Topic[V]) registration() registered {
	return registered{name: t.Name, policy: t.Policy, run: func(ctx context.Context, into producer) error {
		return t.Source.Run(ctx, typedSink[V]{producer: into, entry: t.Entry})
	}}
}

// Sampled adapts a sampling function to a Source for a one-key topic. Samples
// never overlap, so a slow one lowers the rate and cannot stack. sample should
// return by the deadline of its context, reporting anything it could not read
// inside its value; an error ends the topic.
//
// A sample that ignores its context cannot be killed. Once it has overrun by
// StopGrace the topic fails with a typed failure, and the source runs no other
// sample until that one returns, however often the topic is subscribed again, so
// it costs one goroutine and no more.
func Sampled[V proto.Message](key string, cadence, deadline time.Duration, ticks clock.Clock, sample func(context.Context) (V, error)) Source[V] {
	if cadence <= 0 || deadline <= 0 {
		panic(fmt.Sprintf("sampled topic %s needs a positive cadence and deadline, got %s and %s", key, cadence, deadline))
	}
	return &sampled[V]{key: key, cadence: cadence, deadline: deadline, ticks: ticks, sample: sample}
}

type sampled[V proto.Message] struct {
	key               string
	cadence, deadline time.Duration
	ticks             clock.Clock
	sample            func(context.Context) (V, error)
	// inflight is held from a sample's start until it returns. It belongs to the
	// source, which outlives each producer run.
	inflight atomic.Bool
}

func (s *sampled[V]) Run(ctx context.Context, sink Sink[V]) error {
	ticker := s.ticks.NewTicker(s.cadence)
	defer ticker.Stop()
	for {
		value, err := s.once(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			return err
		}
		sink.Put(s.key, value)
		sink.Synced()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.Ticks():
		}
	}
}

// once takes one sample, ending its context at the deadline or when ctx ends.
func (s *sampled[V]) once(ctx context.Context) (V, error) {
	var none V
	if !s.inflight.CompareAndSwap(false, true) {
		return none, &Failure{Code: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, Detail: "an earlier sample has not returned"}
	}
	sampleCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	type outcome struct {
		value V
		err   error
	}
	done := make(chan outcome, 1)
	go func() {
		defer s.inflight.Store(false)
		defer func() {
			if failed := recover(); failed != nil {
				done <- outcome{err: &Failure{Code: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL, Detail: fmt.Sprintf("panic: %v", failed)}}
			}
		}()
		value, err := s.sample(sampleCtx)
		done <- outcome{value, err}
	}()
	deadline := s.ticks.NewTicker(s.deadline)
	defer deadline.Stop()
	select {
	case result := <-done:
		return result.value, result.err
	case <-deadline.Ticks():
	case <-ctx.Done():
	}
	cancel()
	grace := s.ticks.NewTicker(StopGrace)
	defer grace.Stop()
	select {
	case result := <-done:
		return result.value, result.err
	case <-grace.Ticks():
		return none, &Failure{Code: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
			Detail: fmt.Sprintf("sample did not return within %s of its deadline", StopGrace)}
	}
}

// failedFrame sanitizes a code and a detail, which may be a source's own choice,
// a panic value or a tool's output, into what the schema accepts. A source that
// names no defined code has a defect, which is INTERNAL.
func failedFrame(code apiv1.TopicFailureCode, detail string) *apiv1.TopicFailure {
	if _, defined := apiv1.TopicFailureCode_name[int32(code)]; !defined || code == apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSPECIFIED {
		detail = fmt.Sprintf("source chose failure code %d: %s", int32(code), detail)
		code = apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL
	}
	detail = strings.ToValidUTF8(detail, "?")
	if len(detail) > maxDetailBytes {
		cut := maxDetailBytes
		for cut > 0 && !utf8.RuneStart(detail[cut]) {
			cut--
		}
		detail = detail[:cut]
	}
	return failure(code, detail)
}

func failureOf(err error) *apiv1.TopicFailure {
	var typed *Failure
	if errors.As(err, &typed) {
		return failedFrame(typed.Code, typed.Detail)
	}
	return failedFrame(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, err.Error())
}
