package trainticket

import (
	"fmt"
	"math/rand"

	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

type entity map[string]any

type graphCase struct {
	index               int
	config              entity
	stationA            entity
	stationB            entity
	train               entity
	routeInput          entity
	route               entity
	price               entity
	tripInput           entity
	trip                entity
	retiredStationNames map[string]string
	retiredRouteKeys    [][2]string
	retiredPriceKeys    [][2]string
	journalEntries      []string
	mutationEpoch       int
}

func makeCase(random *rand.Rand, namespace string, index int) *graphCase {
	token := trainticketsupport.Token(random, namespace, index)
	stationA := entity{
		"id":       token + "a",
		"name":     trainticketsupport.StationName(random, false, token),
		"stayTime": trainticketsupport.StationStayTime(random),
	}
	stationB := entity{
		"id":       token + "b",
		"name":     trainticketsupport.StationName(random, true, token),
		"stayTime": trainticketsupport.StationStayTime(random),
	}
	train := entity{
		"id":           "T" + token,
		"economyClass": trainticketsupport.TrainEconomyClass(random),
		"confortClass": trainticketsupport.TrainConfortClass(random),
		"averageSpeed": trainticketsupport.TrainAverageSpeed(random),
	}
	routeID := trainticketsupport.UUID(random)
	distance := trainticketsupport.RouteDistance(random)
	routeInput := entity{
		"id":           routeID,
		"startStation": stationA["id"],
		"endStation":   stationB["id"],
		"stationList":  fmt.Sprintf("%s,%s", stationA["id"], stationB["id"]),
		"distanceList": fmt.Sprintf("0,%d", distance),
	}
	route := entity{
		"id":                routeID,
		"stations":          []string{stringValue(stationA, "id"), stringValue(stationB, "id")},
		"distances":         []int{0, distance},
		"startStationId":    stationA["id"],
		"terminalStationId": stationB["id"],
	}
	basicRate, firstClassRate := trainticketsupport.PriceRates(random)
	price := entity{
		"id":                  trainticketsupport.UUID(random),
		"trainType":           train["id"],
		"routeId":             routeID,
		"basicPriceRate":      basicRate,
		"firstClassPriceRate": firstClassRate,
	}
	tripType, tripNumber := trainticketsupport.TripIdentity(random)
	tripID := tripType + tripNumber
	startingTime, endTime := trainticketsupport.TripTimes(random)
	tripInput := entity{
		"tripId":            tripID,
		"trainTypeId":       train["id"],
		"routeId":           routeID,
		"startingStationId": stationA["id"],
		"stationsId":        stationB["id"],
		"terminalStationId": stationB["id"],
		"startingTime":      startingTime,
		"endTime":           endTime,
	}
	trip := cloneEntity(tripInput)
	trip["tripId"] = entity{"type": tripType, "number": tripNumber}
	config := entity{
		"name":        trainticketsupport.ConfigName(random, token),
		"value":       fmt.Sprintf("v-%016x", random.Uint64()),
		"description": fmt.Sprintf("d-%016x", random.Uint64()),
	}
	return &graphCase{
		index: index, config: config, stationA: stationA, stationB: stationB,
		train: train, routeInput: routeInput, route: route, price: price,
		tripInput: tripInput, trip: trip,
		retiredStationNames: make(map[string]string),
	}
}

func cloneEntity(source entity) entity {
	cloned := make(entity, len(source))
	for key, value := range source {
		cloned[key] = cloneValue(value)
	}
	return cloned
}

func cloneValue(value any) any {
	switch typed := value.(type) {
	case entity:
		return cloneEntity(typed)
	case map[string]any:
		cloned := make(map[string]any, len(typed))
		for key, item := range typed {
			cloned[key] = cloneValue(item)
		}
		return cloned
	case []string:
		return append([]string(nil), typed...)
	case []int:
		return append([]int(nil), typed...)
	case []any:
		cloned := make([]any, len(typed))
		for index, item := range typed {
			cloned[index] = cloneValue(item)
		}
		return cloned
	default:
		return value
	}
}

func stringValue(item entity, field string) string {
	return item[field].(string)
}

func intValue(item entity, field string) int {
	switch value := item[field].(type) {
	case int:
		return value
	case int64:
		return int(value)
	default:
		panic(fmt.Sprintf("trusted entity field %s is %T", field, value))
	}
}

func int64Value(item entity, field string) int64 {
	switch value := item[field].(type) {
	case int:
		return int64(value)
	case int64:
		return value
	default:
		panic(fmt.Sprintf("trusted entity field %s is %T", field, value))
	}
}
