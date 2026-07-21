package trainticket

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"net/url"
	"strings"

	"vibesys/microservice-evaluator/accuracy"
	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/accuracy/jsoncheck"
)

func (a *Application) verifyProtocol(ctx context.Context, client *client) (int, error) {
	checks := 0
	for _, service := range services {
		welcome := welcomePaths[service]
		result := client.request(ctx, service, http.MethodGet, welcome.path, nil, true)
		if err := httpcheck.ExactText(result, http.StatusOK, welcome.text); err != nil {
			return checks, fmt.Errorf("%s welcome: %w", service, err)
		}
		checks++
	}
	cacheResult := client.request(ctx, "station", http.MethodGet, "/stations", nil, true)
	if _, err := httpcheck.ExactEnvelope(cacheResult, http.StatusOK, 1); err != nil {
		return checks, fmt.Errorf("station cache probe: %w", err)
	}
	cacheControl := strings.ToLower(httpcheck.Header(cacheResult, "Cache-Control"))
	if strings.Contains(cacheControl, "public") || strings.Contains(cacheControl, "immutable") {
		return checks, fmt.Errorf("mutable station collection has unsafe Cache-Control %q", cacheControl)
	}
	checks++

	first := client.request(ctx, "station", http.MethodGet, "/stations", nil, true)
	if _, err := httpcheck.ExactEnvelope(first, http.StatusOK, 1); err != nil {
		return checks, fmt.Errorf("first persistent HTTP request: %w", err)
	}
	second := client.request(ctx, "station", http.MethodGet, "/stations", nil, true)
	if _, err := httpcheck.ExactEnvelope(second, http.StatusOK, 1); err != nil {
		return checks, fmt.Errorf("second persistent HTTP request: %w", err)
	}
	if !second.ConnectionKnown || !second.ConnectionReused {
		return checks, fmt.Errorf("HTTP connection was not reused for two sequential requests")
	}
	return checks + 2, nil
}

func (a *Application) verifySeedCatalog(ctx context.Context, client *client) (int, error) {
	return a.verifyExactState(ctx, client, nil)
}

func (a *Application) verifyExactState(
	ctx context.Context,
	client *client,
	live []*graphCase,
) (int, error) {
	checks := 0
	for _, service := range services {
		expected := make([]any, 0, len(a.catalog[service])+2*len(live))
		for _, seed := range a.catalog[service] {
			expected = append(expected, seed)
		}
		for _, item := range live {
			expected = append(expected, caseEntities(service, item)...)
		}
		if err := client.exactList(ctx, service, expected, "exact "+service+" state"); err != nil {
			return checks, err
		}
		checks += len(expected) + 1
	}
	return checks, nil
}

func caseEntities(service string, item *graphCase) []any {
	switch service {
	case "config":
		return []any{item.config}
	case "station":
		return []any{item.stationA, item.stationB}
	case "train":
		return []any{item.train}
	case "route":
		return []any{item.route}
	case "price":
		return []any{item.price}
	default:
		return []any{item.trip}
	}
}

type createStep struct {
	name       string
	service    string
	method     string
	path       string
	body       any
	httpStatus int
	created    func(any) error
	cleanup    func() (string, string, any)
	expected   entity
}

func (a *Application) createCase(
	ctx context.Context,
	client *client,
	journal *accuracy.Journal,
	item *graphCase,
) (int, error) {
	steps := []createStep{
		{
			name: "config", service: "config", method: http.MethodPost, path: "/configs",
			body: item.config, httpStatus: http.StatusCreated, expected: item.config,
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/configs/" + url.PathEscape(stringValue(item.config, "name")), nil
			},
		},
		{
			name: "station-a", service: "station", method: http.MethodPost, path: "/stations",
			body: item.stationA, httpStatus: http.StatusCreated, expected: item.stationA,
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/stations", item.stationA
			},
		},
		{
			name: "station-b", service: "station", method: http.MethodPost, path: "/stations",
			body: item.stationB, httpStatus: http.StatusCreated, expected: item.stationB,
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/stations", item.stationB
			},
		},
		{
			name: "train", service: "train", method: http.MethodPost, path: "/trains",
			body: item.train, httpStatus: http.StatusOK, expected: item.train,
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/trains/" + stringValue(item.train, "id"), nil
			},
		},
		{
			name: "route", service: "route", method: http.MethodPost, path: "/routes",
			body: item.routeInput, httpStatus: http.StatusOK, expected: item.route,
			created: func(data any) error {
				return assertEntity("route", data, item.route, "route create response")
			},
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/routes/" + stringValue(item.route, "id"), nil
			},
		},
		{
			name: "price", service: "price", method: http.MethodPost, path: "/prices",
			body: item.price, httpStatus: http.StatusCreated, expected: item.price,
			created: func(data any) error {
				return assertEntity("price", data, item.price, "price create response")
			},
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/prices", item.price
			},
		},
		{
			name: "travel", service: "travel", method: http.MethodPost, path: "/trips",
			body: item.tripInput, httpStatus: http.StatusCreated, expected: item.trip,
			cleanup: func() (string, string, any) {
				return http.MethodDelete, "/trips/" + stringValue(item.tripInput, "tripId"), nil
			},
		},
	}
	checks := 0
	for _, step := range steps {
		entryName := fmt.Sprintf("case-%d/%s", item.index, step.name)
		cleanupMethod, cleanupPath, cleanupBody := step.cleanup()
		// Own cleanup before issuing a mutating request. A timeout or malformed
		// response cannot prove that the server did not apply the create.
		if err := recordCleanup(
			journal, item, entryName, client, step.service,
			cleanupMethod, cleanupPath, cleanupBody, step.expected,
		); err != nil {
			return checks, err
		}
		data, err := client.envelope(
			ctx, step.service, step.method, step.path, step.body, step.httpStatus, 1,
		)
		if err != nil {
			return checks, err
		}
		if step.created != nil {
			if err := step.created(data); err != nil {
				return checks, err
			}
		}
		checks++
	}
	return checks, nil
}

func recordCleanup(
	journal *accuracy.Journal,
	item *graphCase,
	name string,
	client *client,
	service, method, path string,
	body any,
	expected entity,
) error {
	ownedBody := snapshotValue(body)
	ownedEntity := cloneEntity(expected)
	if err := journal.Record(name, func(cleanupContext context.Context) error {
		return cleanupOwnedEntity(
			cleanupContext, client, service, method, path, ownedBody, ownedEntity,
		)
	}); err != nil {
		return err
	}
	item.journalEntries = append(item.journalEntries, name)
	return nil
}

func snapshotValue(value any) any {
	if object, ok := value.(entity); ok {
		return cloneEntity(object)
	}
	return value
}

func cleanupOwnedEntity(
	ctx context.Context,
	client *client,
	service, method, path string,
	body any,
	expected entity,
) error {
	result := client.request(ctx, service, method, path, body, true)
	if _, err := httpcheck.EnvelopeStatusIn(result, http.StatusOK, 0, 1); err != nil {
		return err
	}
	listed, err := client.list(ctx, service)
	if err != nil {
		return err
	}
	expectedObject, err := normalizedObject(expected)
	if err != nil {
		return err
	}
	expectedIdentity, err := cleanupIdentity(service, expectedObject)
	if err != nil {
		return err
	}
	for _, actual := range listed {
		identity, identityErr := cleanupIdentity(service, actual)
		if identityErr != nil {
			return identityErr
		}
		if identity == expectedIdentity {
			return fmt.Errorf("cleanup %s identity %q remains visible", service, expectedIdentity)
		}
	}
	return nil
}

func cleanupIdentity(service string, object map[string]any) (string, error) {
	if service == "price" {
		route, routeOK := object["routeId"].(string)
		train, trainOK := object["trainType"].(string)
		if !routeOK || !trainOK {
			return "", fmt.Errorf("cleanup price compound identity is malformed")
		}
		return route + "\x00" + train, nil
	}
	return contracts[service].Key(object)
}

func (a *Application) verifyCase(
	ctx context.Context,
	client *client,
	item *graphCase,
	random *rand.Rand,
) (int, error) {
	type checkFunc func() error
	checks := []checkFunc{
		func() error {
			path := "/configs/" + url.PathEscape(stringValue(item.config, "name"))
			data, err := client.envelope(ctx, "config", http.MethodGet, path, nil, 200, 1)
			if err != nil {
				return err
			}
			if err := assertEntity("config", data, item.config, "config read-your-write"); err != nil {
				return err
			}
			return assertListed(ctx, client, "config", item.config, "config list")
		},
		func() error { return verifyStation(ctx, client, item.stationA) },
		func() error { return verifyStation(ctx, client, item.stationB) },
		func() error {
			path := "/trains/" + stringValue(item.train, "id")
			data, err := client.envelope(ctx, "train", http.MethodGet, path, nil, 200, 1)
			if err != nil {
				return err
			}
			if err := assertEntity("train", data, item.train, "train read-your-write"); err != nil {
				return err
			}
			return assertListed(ctx, client, "train", item.train, "train list")
		},
		func() error { return verifyRoute(ctx, client, item) },
		func() error { return verifyPrice(ctx, client, item) },
		func() error { return verifyTrip(ctx, client, item) },
	}
	if len(item.retiredStationNames)+len(item.retiredRouteKeys)+len(item.retiredPriceKeys) > 0 {
		checks = append(checks, func() error {
			_, err := verifyRetiredSecondaryIndexes(ctx, client, item)
			return err
		})
	}
	random.Shuffle(len(checks), func(left, right int) { checks[left], checks[right] = checks[right], checks[left] })
	for index, check := range checks {
		if err := check(); err != nil {
			return index, err
		}
	}
	return len(checks), nil
}

func verifyStation(ctx context.Context, client *client, station entity) error {
	if err := assertListed(ctx, client, "station", station, "station read-your-write"); err != nil {
		return err
	}
	stationID := stringValue(station, "id")
	stationName := stringValue(station, "name")
	byID, err := client.envelope(ctx, "station", http.MethodGet, "/stations/name/"+stationID, nil, 200, 1)
	if err != nil {
		return err
	}
	if byID != stationName {
		return fmt.Errorf("station name lookup returned %v, expected %q", byID, stationName)
	}
	byName, err := client.envelope(
		ctx, "station", http.MethodGet, "/stations/id/"+url.PathEscape(stationName), nil, 200, 1,
	)
	if err != nil {
		return err
	}
	if byName != stationID {
		return fmt.Errorf("station ID lookup returned %v, expected %q", byName, stationID)
	}
	return nil
}

func verifyRoute(ctx context.Context, client *client, item *graphCase) error {
	routeID := stringValue(item.route, "id")
	data, err := client.envelope(ctx, "route", http.MethodGet, "/routes/"+routeID, nil, 200, 1)
	if err != nil {
		return err
	}
	if err := assertEntity("route", data, item.route, "route read-your-write"); err != nil {
		return err
	}
	path := fmt.Sprintf(
		"/routes/%s/%s",
		stringValue(item.route, "startStationId"),
		stringValue(item.route, "terminalStationId"),
	)
	found, err := client.envelope(ctx, "route", http.MethodGet, path, nil, 200, 1)
	if err != nil {
		return err
	}
	if _, err := contracts["route"].ExactList(
		found, []any{item.route}, "route secondary lookup",
	); err != nil {
		return err
	}
	return assertListed(ctx, client, "route", item.route, "route list")
}

func verifyPrice(ctx context.Context, client *client, item *graphCase) error {
	path := fmt.Sprintf(
		"/prices/%s/%s",
		stringValue(item.price, "routeId"),
		stringValue(item.price, "trainType"),
	)
	data, err := client.envelope(ctx, "price", http.MethodGet, path, nil, 200, 1)
	if err != nil {
		return err
	}
	if err := assertEntity("price", data, item.price, "price read-your-write"); err != nil {
		return err
	}
	return assertListed(ctx, client, "price", item.price, "price list")
}

func verifyTrip(ctx context.Context, client *client, item *graphCase) error {
	tripID := stringValue(item.tripInput, "tripId")
	data, err := client.envelope(ctx, "travel", http.MethodGet, "/trips/"+tripID, nil, 200, 1)
	if err != nil {
		return err
	}
	if err := assertEntity("travel", data, item.trip, "trip read-your-write"); err != nil {
		return err
	}
	return assertListed(ctx, client, "travel", item.trip, "trip list")
}

func assertListed(
	ctx context.Context,
	client *client,
	service string,
	expected entity,
	where string,
) error {
	listed, err := client.list(ctx, service)
	if err != nil {
		return err
	}
	normalized, err := jsoncheck.Normalize(expected)
	if err != nil {
		return err
	}
	key, err := contracts[service].Key(normalized.(map[string]any))
	if err != nil {
		return err
	}
	actual, exists := listed[key]
	if !exists {
		return fmt.Errorf("%s omitted key %q", where, key)
	}
	return assertEntity(service, actual, expected, where)
}
