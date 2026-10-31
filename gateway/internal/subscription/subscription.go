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
	"time"
	"unicode/utf8"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// HeartbeatInterval is the longest gap between frames on a healthy stream.
const HeartbeatInterval = 5 * time.Second

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

// Source produces one topic's facts. Run returns when ctx ends. Any earlier
// return ends the stream for every subscriber: an error as SOURCE_FAILED, or
// its own code through Failure, and a panic as INTERNAL.
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
// never overlap, so a slow one lowers the rate and cannot stack. sample must
// return by the deadline of its context, reporting anything it could not read
// inside its value; an error ends the topic.
func Sampled[V proto.Message](key string, cadence, deadline time.Duration, ticks clock.Clock, sample func(context.Context) (V, error)) Source[V] {
	return sampled[V]{key, cadence, deadline, ticks, sample}
}

type sampled[V proto.Message] struct {
	key               string
	cadence, deadline time.Duration
	ticks             clock.Clock
	sample            func(context.Context) (V, error)
}

func (s sampled[V]) Run(ctx context.Context, sink Sink[V]) error {
	ticker := s.ticks.NewTicker(s.cadence)
	defer ticker.Stop()
	for {
		sampleCtx, cancel := context.WithTimeout(ctx, s.deadline)
		value, err := s.sample(sampleCtx)
		cancel()
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

// failedFrame sanitizes a detail, which may be a panic value or a tool's output,
// into what the schema accepts.
func failedFrame(code apiv1.TopicFailureCode, detail string) *apiv1.TopicFailure {
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
