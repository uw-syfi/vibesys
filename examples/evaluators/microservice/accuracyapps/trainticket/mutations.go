package trainticket

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"net/url"

	"vibesys/microservice-evaluator/accuracy"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

func (a *Application) updateCase(
	ctx context.Context,
	client *client,
	journal *accuracy.Journal,
	item *graphCase,
	random *rand.Rand,
) (int, error) {
	item.mutationEpoch++
	version := random.Uint64()
	suffix := fmt.Sprintf("%016x", version)
	item.config["value"] = suffix
	item.config["description"] = fmt.Sprintf("updated-%016x", random.Uint64())

	item.retiredStationNames[stringValue(item.stationA, "name")] = stringValue(item.stationA, "id")
	item.retiredStationNames[stringValue(item.stationB, "name")] = stringValue(item.stationB, "id")
	item.stationA["name"] = trainticketsupport.UpdatedStationName(version, false)
	item.stationA["stayTime"] = trainticketsupport.UpdatedStationStayTime(version)
	item.stationB["name"] = trainticketsupport.UpdatedStationName(version, true)
	item.stationB["stayTime"] = trainticketsupport.UpdatedStationStayTime(version >> 8)
	delete(item.retiredStationNames, stringValue(item.stationA, "name"))
	delete(item.retiredStationNames, stringValue(item.stationB, "name"))
	item.train["averageSpeed"] = trainticketsupport.UpdatedTrainSpeed(version)
	item.train["economyClass"] = trainticketsupport.UpdatedTrainEconomy(intValue(item.train, "economyClass"))

	item.retiredRouteKeys = append(item.retiredRouteKeys, [2]string{
		stringValue(item.route, "startStationId"),
		stringValue(item.route, "terminalStationId"),
	})
	stationAID := stringValue(item.stationA, "id")
	stationBID := stringValue(item.stationB, "id")
	startID, terminalID := stationAID, stationBID
	if stringValue(item.route, "startStationId") == stationAID {
		startID, terminalID = stationBID, stationAID
	}
	item.route["stations"] = []string{startID, terminalID}
	item.route["startStationId"] = startID
	item.route["terminalStationId"] = terminalID
	item.retiredRouteKeys = removePair(item.retiredRouteKeys, [2]string{startID, terminalID})
	distance := item.route["distances"].([]int)
	distance[1] = trainticketsupport.UpdatedRouteDistance(distance[1], version)
	item.route["distances"] = distance
	item.routeInput["startStation"] = startID
	item.routeInput["endStation"] = terminalID
	item.routeInput["stationList"] = fmt.Sprintf("%s,%s", startID, terminalID)
	item.routeInput["distanceList"] = fmt.Sprintf("0,%d", distance[1])

	item.retiredPriceKeys = append(item.retiredPriceKeys, [2]string{
		stringValue(item.price, "routeId"), stringValue(item.price, "trainType"),
	})
	// Keep one runtime-unique component so the new compound key cannot collide
	// with the seeded price catalog or a different randomized graph.
	if item.index%2 == 0 {
		item.price["routeId"] = differentCatalogID(
			random, a.catalog["route"], stringValue(item.price, "routeId"),
		)
		item.price["trainType"] = item.train["id"]
	} else {
		item.price["routeId"] = item.route["id"]
		item.price["trainType"] = differentCatalogID(
			random, a.catalog["train"], stringValue(item.price, "trainType"),
		)
	}
	item.retiredPriceKeys = removePair(item.retiredPriceKeys, [2]string{
		stringValue(item.price, "routeId"), stringValue(item.price, "trainType"),
	})
	item.price["basicPriceRate"], item.price["firstClassPriceRate"] = trainticketsupport.UpdatedPriceRates(version)
	if err := recordCleanup(
		journal,
		item,
		fmt.Sprintf("case-%d/price-updated-%d", item.index, item.mutationEpoch),
		client,
		"price",
		http.MethodDelete,
		"/prices",
		item.price,
		item.price,
	); err != nil {
		return 0, err
	}

	item.tripInput["endTime"] = trainticketsupport.UpdatedTripEnd(int64Value(item.tripInput, "endTime"), version)
	item.tripInput["startingStationId"] = startID
	item.tripInput["stationsId"] = terminalID
	item.tripInput["terminalStationId"] = terminalID
	item.trip["startingStationId"] = startID
	item.trip["stationsId"] = terminalID
	item.trip["terminalStationId"] = terminalID
	item.trip["endTime"] = item.tripInput["endTime"]

	operations := []func() error{
		func() error {
			_, err := client.envelope(ctx, "config", http.MethodPut, "/configs", item.config, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "station", http.MethodPut, "/stations", item.stationA, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "station", http.MethodPut, "/stations", item.stationB, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "train", http.MethodPut, "/trains", item.train, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "route", http.MethodPost, "/routes", item.routeInput, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "price", http.MethodPut, "/prices", item.price, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "travel", http.MethodPut, "/trips", item.tripInput, 200, 1)
			return err
		},
	}
	random.Shuffle(len(operations), func(left, right int) {
		operations[left], operations[right] = operations[right], operations[left]
	})
	for index, operation := range operations {
		if err := operation(); err != nil {
			return index, err
		}
	}
	return len(operations), nil
}

func differentCatalogID(random *rand.Rand, catalog []map[string]any, current string) string {
	start := random.Intn(len(catalog))
	for offset := range catalog {
		candidate := catalog[(start+offset)%len(catalog)]["id"].(string)
		if candidate != current {
			return candidate
		}
	}
	panic("trusted seed catalog does not contain a different ID")
}

func removePair(values [][2]string, remove [2]string) [][2]string {
	filtered := values[:0]
	for _, value := range values {
		if value != remove {
			filtered = append(filtered, value)
		}
	}
	return filtered
}

func verifyRetiredSecondaryIndexes(
	ctx context.Context,
	client *client,
	item *graphCase,
) (int, error) {
	checks := 0
	for name := range item.retiredStationNames {
		if _, err := client.envelope(
			ctx, "station", http.MethodGet, "/stations/id/"+url.PathEscape(name), nil, 200, 0,
		); err != nil {
			return checks, err
		}
		checks++
	}
	for _, key := range item.retiredRouteKeys {
		if _, err := client.envelope(
			ctx, "route", http.MethodGet, "/routes/"+key[0]+"/"+key[1], nil, 200, 0,
		); err != nil {
			return checks, err
		}
		checks++
	}
	for _, key := range item.retiredPriceKeys {
		if _, err := client.envelope(
			ctx, "price", http.MethodGet, "/prices/"+key[0]+"/"+key[1], nil, 200, 0,
		); err != nil {
			return checks, err
		}
		checks++
	}
	return checks, nil
}

func (a *Application) deleteCase(
	ctx context.Context,
	client *client,
	item *graphCase,
	random *rand.Rand,
) (int, error) {
	dependent := []func() error{
		func() error {
			_, err := client.envelope(
				ctx, "travel", http.MethodDelete, "/trips/"+stringValue(item.tripInput, "tripId"), nil, 200, 1,
			)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "price", http.MethodDelete, "/prices", item.price, 200, 1)
			return err
		},
	}
	random.Shuffle(len(dependent), func(left, right int) {
		dependent[left], dependent[right] = dependent[right], dependent[left]
	})
	checks := 0
	for _, operation := range dependent {
		if err := operation(); err != nil {
			return checks, err
		}
		checks++
	}
	if _, err := client.envelope(
		ctx, "route", http.MethodDelete, "/routes/"+stringValue(item.route, "id"), nil, 200, 1,
	); err != nil {
		return checks, err
	}
	checks++
	independent := []func() error{
		func() error {
			_, err := client.envelope(
				ctx, "train", http.MethodDelete, "/trains/"+stringValue(item.train, "id"), nil, 200, 1,
			)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "station", http.MethodDelete, "/stations", item.stationA, 200, 1)
			return err
		},
		func() error {
			_, err := client.envelope(ctx, "station", http.MethodDelete, "/stations", item.stationB, 200, 1)
			return err
		},
		func() error {
			path := "/configs/" + url.PathEscape(stringValue(item.config, "name"))
			_, err := client.envelope(ctx, "config", http.MethodDelete, path, nil, 200, 1)
			return err
		},
	}
	random.Shuffle(len(independent), func(left, right int) {
		independent[left], independent[right] = independent[right], independent[left]
	})
	for _, operation := range independent {
		if err := operation(); err != nil {
			return checks, err
		}
		checks++
	}
	return checks, nil
}

func (a *Application) verifyDeleted(
	ctx context.Context,
	client *client,
	item *graphCase,
) (int, error) {
	probes := []struct {
		service string
		path    string
	}{
		{"config", "/configs/" + url.PathEscape(stringValue(item.config, "name"))},
		{"train", "/trains/" + stringValue(item.train, "id")},
		{"route", "/routes/" + stringValue(item.route, "id")},
		{
			"route",
			"/routes/" + stringValue(item.route, "startStationId") + "/" +
				stringValue(item.route, "terminalStationId"),
		},
		{"price", "/prices/" + stringValue(item.price, "routeId") + "/" + stringValue(item.price, "trainType")},
		{"travel", "/trips/" + stringValue(item.tripInput, "tripId")},
	}
	checks := 0
	for _, probe := range probes {
		if _, err := client.envelope(ctx, probe.service, http.MethodGet, probe.path, nil, 200, 0); err != nil {
			return checks, err
		}
		checks++
	}
	deleted := map[string][]entity{
		"config": {item.config}, "station": {item.stationA, item.stationB},
		"train": {item.train}, "route": {item.route}, "price": {item.price},
		"travel": {item.trip},
	}
	for _, service := range services {
		listed, err := client.list(ctx, service)
		if err != nil {
			return checks, err
		}
		for _, expected := range deleted[service] {
			normalized, err := normalizedObject(expected)
			if err != nil {
				return checks, err
			}
			key, err := contracts[service].Key(normalized)
			if err != nil {
				return checks, err
			}
			if _, exists := listed[key]; exists {
				return checks, fmt.Errorf("deleted %s %q remains visible in list", service, key)
			}
			checks++
		}
	}
	for _, station := range []entity{item.stationA, item.stationB} {
		stationID := stringValue(station, "id")
		stationName := stringValue(station, "name")
		if _, err := client.envelope(
			ctx, "station", http.MethodGet, "/stations/name/"+stationID, nil, 200, 0,
		); err != nil {
			return checks, err
		}
		if _, err := client.envelope(
			ctx, "station", http.MethodGet, "/stations/id/"+url.PathEscape(stationName), nil, 200, 0,
		); err != nil {
			return checks, err
		}
		checks += 2
	}
	retiredChecks, err := verifyRetiredSecondaryIndexes(ctx, client, item)
	return checks + retiredChecks, err
}

func dismissCase(journal *accuracy.Journal, item *graphCase) error {
	for _, name := range item.journalEntries {
		if err := journal.Dismiss(name); err != nil {
			return err
		}
	}
	item.journalEntries = nil
	return nil
}
