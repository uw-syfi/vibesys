// Package hotel implements the typed benchmark adapter for DeathStarBench's
// Hotel Reservation application.
package hotel

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

const (
	operationSearch            = "search_hotels"
	operationRecommendDistance = "recommend_distance"
	operationRecommendRate     = "recommend_rate"
	operationRecommendPrice    = "recommend_price"
	operationLoginValid        = "login_valid"
	operationLoginInvalid      = "login_invalid"
	operationReserveCapacity   = "reserve_capacity"
)

var knownOperations = map[string]struct{}{
	operationSearch:            {},
	operationRecommendDistance: {},
	operationRecommendRate:     {},
	operationRecommendPrice:    {},
	operationLoginValid:        {},
	operationLoginInvalid:      {},
	operationReserveCapacity:   {},
}

type Config = hotelsupport.Config

type Application struct {
	config Config
	seed   int64

	// A reservation plan holds this lock until FinishOperation. The service's
	// capacity check and write are not transactional, so concurrent fill plans
	// could both observe spare capacity and make the rejection oracle ambiguous.
	reservationMu sync.Mutex
}

type fixture struct {
	dateBase        time.Time
	hotelOffset     uint64
	nextReservation atomic.Uint64
}

const reservationDateBlockDays = 4096

var _ api.Application = (*Application)(nil)
var _ api.PreflightApplication = (*Application)(nil)

func New(workload api.Workload) (api.Application, error) {
	config, err := hotelsupport.ValidateTopology(workload)
	if err != nil {
		return nil, err
	}
	for _, operation := range workload.Operations {
		if _, ok := knownOperations[operation.Name]; !ok {
			return nil, fmt.Errorf("unknown Hotel operation %q", operation.Name)
		}
		if operation.Target != hotelsupport.GatewayTarget {
			return nil, fmt.Errorf("Hotel operation %q must target %q", operation.Name, hotelsupport.GatewayTarget)
		}
		if operation.HTTP != nil {
			return nil, fmt.Errorf("Hotel operation %q must not declare operations.http", operation.Name)
		}
	}
	return &Application{config: config, seed: workload.Load.Seed}, nil
}

func (a *Application) Name() string { return "hotel" }

func (a *Application) ReadinessProbes() []api.ReadinessProbe {
	return hotelsupport.ReadinessProbes(a.seed)
}

func (a *Application) PreflightProbes() []api.ReadinessProbe {
	return hotelsupport.PreflightProbes()
}

func (a *Application) Prepare(_ context.Context, _ api.Runtime, trial api.TrialContext) (any, error) {
	// Reserve only future dates, both to avoid the seeded 2015 reservation and
	// to namespace separate optimization trials without setup-only traffic. A
	// digest selects an aligned block between years 3000 and 7000; alignment
	// prevents partially overlapping namespaces.
	digest := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", trial.FixtureSeed, trial.Index)))
	rangeStart := time.Date(3000, time.January, 1, 12, 0, 0, 0, time.UTC)
	// Ten complete Gregorian 400-year cycles avoid time.Duration's roughly
	// 290-year range limit while retaining exact calendar arithmetic.
	const availableDays = 10 * 146097
	blocks := uint64(availableDays / reservationDateBlockDays)
	block := binary.BigEndian.Uint64(digest[:8]) % blocks
	return &fixture{
		dateBase:    rangeStart.AddDate(0, 0, int(block)*reservationDateBlockDays),
		hotelOffset: binary.BigEndian.Uint64(digest[8:16]) % 80,
	}, nil
}

func (a *Application) Reset(context.Context, api.Runtime, api.TrialContext) error {
	// DeathStarBench exposes no reservation deletion API. The fixture therefore
	// gives every trial a separate namespace and every scheduled sample
	// receives a unique date within that trial.
	return nil
}
