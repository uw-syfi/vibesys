package trainticket

import (
	"fmt"
	"math"
	"math/rand"
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
}

func makeCase(random *rand.Rand, namespace string, index int) *graphCase {
	token := fmt.Sprintf("%s%03x%08x", namespace, index, random.Uint32())
	stationA := entity{
		"id":       token + "a",
		"name":     choose(random, "Station A ", "North Hub ", "北站 ") + token,
		"stayTime": 1 + random.Intn(40),
	}
	stationB := entity{
		"id":       token + "b",
		"name":     choose(random, "Station B ", "South Hub ", "南站 ") + token,
		"stayTime": 1 + random.Intn(40),
	}
	train := entity{
		"id":           "T" + token,
		"economyClass": 100 + random.Intn(801),
		"confortClass": 50 + random.Intn(251),
		"averageSpeed": 80 + random.Intn(271),
	}
	routeID := randomUUID(random)
	distance := 100 + random.Intn(1701)
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
	price := entity{
		"id":                  randomUUID(random),
		"trainType":           train["id"],
		"routeId":             routeID,
		"basicPriceRate":      round4(0.1 + random.Float64()*0.8),
		"firstClassPriceRate": round4(0.9 + random.Float64()),
	}
	tripType := choose(random, "G", "D")
	tripNumber := fmt.Sprintf("%07d", 1_000_000+random.Intn(9_000_000))
	tripID := tripType + tripNumber
	startingTime := int64(1_600_000_000_000) + random.Int63n(300_000_000_001)
	endTime := startingTime + int64(3_600_000+random.Intn(39_600_001))
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
		"name":        choose(random, token+"Config", "config "+token, "配置-"+token),
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

func randomUUID(random *rand.Rand) string {
	var value [16]byte
	_, _ = random.Read(value[:])
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	return fmt.Sprintf(
		"%08x-%04x-%04x-%04x-%012x",
		value[0:4], value[4:6], value[6:8], value[8:10], value[10:16],
	)
}

func choose(random *rand.Rand, values ...string) string {
	return values[random.Intn(len(values))]
}

func round4(value float64) float64 {
	return math.Round(value*10_000) / 10_000
}

func cloneEntity(source entity) entity {
	cloned := make(entity, len(source))
	for key, value := range source {
		cloned[key] = value
	}
	return cloned
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
