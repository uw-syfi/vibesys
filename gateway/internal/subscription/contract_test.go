package subscription_test

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
)

// The case names a defect must be caught by, so a case that goes vacuous shows
// as its wrong topic passing.
const (
	caseValues  = "values refresh inside the stale bound and no faster than the cadence"
	caseAgain   = "subscribing again and again samples no faster than the cadence"
	caseHealthy = "a healthy run never fails and never goes quiet"
	caseStuck   = "a stuck sampler is a typed absence or failure and costs nothing per retry"
)

var generated = hostoperations.Topics[hostoperations.TopicMachineStats]

// each is a sample function that keeps no state between runs, as a Sampled begin.
func each(sample func(context.Context) (stats, error)) func() (func(context.Context) (stats, error), error) {
	return func() (func(context.Context) (stats, error), error) { return sample, nil }
}

// sampler is a working sampled source: each sample is a new value, as a sample's
// own timestamp makes it.
func sampler(cadence, deadline time.Duration) subscription.Source[stats] {
	var samples atomic.Int64
	return subscription.Sampled("machine", cadence, deadline, clock.System{}, each(func(context.Context) (stats, error) {
		return value(samples.Add(1)), nil
	}))
}

// release is a gate that opens once, however often it is called.
type release struct {
	once sync.Once
	gate chan struct{}
}

func newRelease() *release { return &release{gate: make(chan struct{})} }

func (r *release) open() { r.once.Do(func() { close(r.gate) }) }

// stuckSampler is a sampled source whose sample never returns and ignores its
// context until released: what a sample inside a stuck tool does.
func stuckSampler() (subscription.Source[stats], func()) {
	gate := newRelease()
	source := subscription.Sampled("machine", generated.Cadence, 5*time.Second, clock.System{}, each(func(context.Context) (stats, error) {
		<-gate.gate
		return value(1), nil
	}))
	return source, gate.open
}

// subject is a topic on the generated policy. It builds its source for each case,
// because a bubble has its own time and a source remembers when it last sampled.
func subject(source func() subscription.Source[stats], stuck func() (subscription.Source[stats], func())) func() subscriptiontest.Subject[stats] {
	return func() subscriptiontest.Subject[stats] {
		fake := topic(source())
		fake.Policy = generated
		return subscriptiontest.Subject[stats]{
			Topic:   fake,
			Request: request,
			Failing: script(func(context.Context, subscription.Sink[stats]) error { return errors.New("broken") }),
			Failure: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
			Stuck:   stuck,
		}
	}
}

// working is a new working sampled source at the generated cadence.
func working() subscription.Source[stats] { return sampler(generated.Cadence, 5*time.Second) }

func TestAWorkingSampledTopicPassesTheContract(t *testing.T) {
	subscriptiontest.Contract(t, subject(working, stuckSampler))
}

// Each of these once passed the contract, which checked the hub and not the
// topic. Every one must now fail, in the case that names its defect. Another
// case may fail too: a topic that dies also stops refreshing.
func TestTheContractRejectsTheTopicsThatAreWrong(t *testing.T) {
	cooperative := func() (subscription.Source[stats], func()) {
		gate := newRelease()
		source := subscription.Sampled("machine", generated.Cadence, time.Hour, clock.System{}, each(func(ctx context.Context) (stats, error) {
			select {
			case <-ctx.Done():
			case <-gate.gate:
			}
			return value(1), nil
		}))
		return source, gate.open
	}
	silentWhenStuck := func() (subscription.Source[stats], func()) {
		gate := newRelease()
		return script(func(context.Context, subscription.Sink[stats]) error { <-gate.gate; return errors.New("released") }), gate.open
	}
	wrong := map[string]struct {
		build  func() subscriptiontest.Subject[stats]
		caught string
	}{
		"puts once and never refreshes": {
			subject(func() subscription.Source[stats] {
				return script(func(ctx context.Context, sink subscription.Sink[stats]) error {
					sink.Put("machine", value(1))
					sink.Synced()
					return wait(ctx)
				})
			}, stuckSampler), caseValues,
		},
		"fails after 25 s, past every sleep that used to check": {
			subject(func() subscription.Source[stats] {
				return script(func(ctx context.Context, sink subscription.Sink[stats]) error {
					bounded, cancel := context.WithTimeout(ctx, 25*time.Second)
					defer cancel()
					_ = working().Run(bounded, sink)
					return errors.New("died")
				})
			}, stuckSampler), caseHealthy,
		},
		"samples at 100 times its cadence": {
			subject(func() subscription.Source[stats] { return sampler(generated.Cadence/100, 5*time.Second) }, stuckSampler), caseValues,
		},
		"samples at once for every subscription, as one rebuilt for each would": {
			subject(func() subscription.Source[stats] {
				return script(func(ctx context.Context, sink subscription.Sink[stats]) error { return working().Run(ctx, sink) })
			}, stuckSampler), caseAgain,
		},
		"waits an hour for a stuck sample": {
			subject(working, cooperative), caseStuck,
		},
		"sends heartbeats and nothing else when stuck": {
			subject(working, silentWhenStuck), caseStuck,
		},
		"leaks a goroutine for each subscription to a stuck sampler": {
			subject(working, func() (subscription.Source[stats], func()) {
				gate := newRelease()
				return script(func(ctx context.Context, sink subscription.Sink[stats]) error {
					go func() { <-gate.gate }()
					return working().Run(ctx, sink)
				}), gate.open
			}), caseStuck,
		},
	}
	for name, each := range wrong {
		t.Run(name, func(t *testing.T) {
			if found := subscriptiontest.Violations(t, each.build); found[each.caught] == nil {
				t.Errorf("case %q passed", each.caught)
			}
		})
	}
}
