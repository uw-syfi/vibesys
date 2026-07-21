package trainticket

import (
	"fmt"
	"math/rand"
	"sync"
	"time"

	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

type configEntity struct {
	Name        string `json:"name"`
	Value       string `json:"value"`
	Description string `json:"description"`
}

type stationEntity struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	StayTime int    `json:"stayTime"`
}

type trainEntity struct {
	ID           string `json:"id"`
	EconomyClass int    `json:"economyClass"`
	ConfortClass int    `json:"confortClass"`
	AverageSpeed int    `json:"averageSpeed"`
}

type routeInput struct {
	ID           string `json:"id"`
	StartStation string `json:"startStation"`
	EndStation   string `json:"endStation"`
	StationList  string `json:"stationList"`
	DistanceList string `json:"distanceList"`
}

type routeEntity struct {
	ID                string   `json:"id"`
	Stations          []string `json:"stations"`
	Distances         []int    `json:"distances"`
	StartStationID    string   `json:"startStationId"`
	TerminalStationID string   `json:"terminalStationId"`
}

type priceEntity struct {
	ID                  string  `json:"id"`
	TrainType           string  `json:"trainType"`
	RouteID             string  `json:"routeId"`
	BasicPriceRate      float64 `json:"basicPriceRate"`
	FirstClassPriceRate float64 `json:"firstClassPriceRate"`
}

type tripID struct {
	Type   string `json:"type"`
	Number string `json:"number"`
}

type tripInput struct {
	TripID            string `json:"tripId"`
	TrainTypeID       string `json:"trainTypeId"`
	RouteID           string `json:"routeId"`
	StartingTime      int64  `json:"startingTime"`
	StartingStationID string `json:"startingStationId"`
	StationsID        string `json:"stationsId"`
	TerminalStationID string `json:"terminalStationId"`
	EndTime           int64  `json:"endTime"`
}

type tripEntity struct {
	TripID            tripID `json:"tripId"`
	TrainTypeID       string `json:"trainTypeId"`
	RouteID           string `json:"routeId"`
	StartingTime      int64  `json:"startingTime"`
	StartingStationID string `json:"startingStationId"`
	StationsID        string `json:"stationsId"`
	TerminalStationID string `json:"terminalStationId"`
	EndTime           int64  `json:"endTime"`
}

type record struct {
	mu       sync.RWMutex
	created  [7]bool
	config   configEntity
	stationA stationEntity
	stationB stationEntity
	train    trainEntity
	routeIn  routeInput
	route    routeEntity
	price    priceEntity
	tripIn   tripInput
	trip     tripEntity
}

type dataset struct {
	namespace string
	records   []record
}

func makeRecords(namespace string, seed int64, count int) []record {
	rng := rand.New(rand.NewSource(seed))
	records := make([]record, count)
	for index := range records {
		token := trainticketsupport.Token(rng, namespace, index)
		stationA := stationEntity{ID: token + "a", Name: trainticketsupport.StationName(rng, false, token), StayTime: trainticketsupport.StationStayTime(rng)}
		stationB := stationEntity{ID: token + "b", Name: trainticketsupport.StationName(rng, true, token), StayTime: trainticketsupport.StationStayTime(rng)}
		train := trainEntity{ID: "T" + token, EconomyClass: trainticketsupport.TrainEconomyClass(rng), ConfortClass: trainticketsupport.TrainConfortClass(rng), AverageSpeed: trainticketsupport.TrainAverageSpeed(rng)}
		routeID := trainticketsupport.UUID(rng)
		distance := trainticketsupport.RouteDistance(rng)
		route := routeEntity{ID: routeID, Stations: []string{stationA.ID, stationB.ID}, Distances: []int{0, distance}, StartStationID: stationA.ID, TerminalStationID: stationB.ID}
		routeIn := routeInput{ID: routeID, StartStation: stationA.ID, EndStation: stationB.ID, StationList: stationA.ID + "," + stationB.ID, DistanceList: fmt.Sprintf("0,%d", distance)}
		basicRate, firstClassRate := trainticketsupport.PriceRates(rng)
		price := priceEntity{ID: trainticketsupport.UUID(rng), TrainType: train.ID, RouteID: routeID, BasicPriceRate: basicRate, FirstClassPriceRate: firstClassRate}
		kind, number := trainticketsupport.TripIdentity(rng)
		tripString := kind + number
		start, end := trainticketsupport.TripTimes(rng)
		tripIn := tripInput{TripID: tripString, TrainTypeID: train.ID, RouteID: routeID, StartingTime: start, StartingStationID: stationA.ID, StationsID: stationB.ID, TerminalStationID: stationB.ID, EndTime: end}
		trip := tripEntity{TripID: tripID{Type: kind, Number: number}, TrainTypeID: train.ID, RouteID: routeID, StartingTime: start, StartingStationID: stationA.ID, StationsID: stationB.ID, TerminalStationID: stationB.ID, EndTime: tripIn.EndTime}
		records[index] = record{
			config:   configEntity{Name: trainticketsupport.ConfigName(rng, token), Value: fmt.Sprintf("v-%016x", rng.Uint64()), Description: fmt.Sprintf("d-%016x", rng.Uint64())},
			stationA: stationA, stationB: stationB, train: train,
			routeIn: routeIn, route: route, price: price, tripIn: tripIn, trip: trip,
		}
	}
	return records
}

func makeAdminToken(now time.Time) string {
	token, err := trainticketsupport.AdminToken(now)
	if err != nil {
		panic(err)
	}
	return token
}
