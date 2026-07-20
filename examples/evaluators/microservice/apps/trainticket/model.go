package trainticket

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math/rand"
	"sync"
	"time"
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
	mu       sync.Mutex
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
	prepared  int
}

func makeRecords(namespace string, seed int64, count int) []record {
	rng := rand.New(rand.NewSource(seed))
	records := make([]record, count)
	for index := range records {
		token := fmt.Sprintf("%s%03x%08x", namespace, index, rng.Uint32())
		stationA := stationEntity{ID: token + "a", Name: "Station A " + token, StayTime: 1 + rng.Intn(40)}
		stationB := stationEntity{ID: token + "b", Name: "Station B " + token, StayTime: 1 + rng.Intn(40)}
		train := trainEntity{ID: "T" + token, EconomyClass: 100 + rng.Intn(800), ConfortClass: 50 + rng.Intn(250), AverageSpeed: 80 + rng.Intn(270)}
		routeID := deterministicUUID(rng)
		distance := 100 + rng.Intn(1700)
		route := routeEntity{ID: routeID, Stations: []string{stationA.ID, stationB.ID}, Distances: []int{0, distance}, StartStationID: stationA.ID, TerminalStationID: stationB.ID}
		routeIn := routeInput{ID: routeID, StartStation: stationA.ID, EndStation: stationB.ID, StationList: stationA.ID + "," + stationB.ID, DistanceList: fmt.Sprintf("0,%d", distance)}
		price := priceEntity{ID: deterministicUUID(rng), TrainType: train.ID, RouteID: routeID, BasicPriceRate: 0.1 + rng.Float64()*0.8, FirstClassPriceRate: 0.9 + rng.Float64()}
		kind := "G"
		if rng.Intn(2) == 0 {
			kind = "D"
		}
		number := fmt.Sprintf("%07d", 1_000_000+rng.Intn(8_999_999))
		tripString := kind + number
		start := int64(1_700_000_000_000 + rng.Intn(100_000_000))
		tripIn := tripInput{TripID: tripString, TrainTypeID: train.ID, RouteID: routeID, StartingTime: start, StartingStationID: stationA.ID, StationsID: stationB.ID, TerminalStationID: stationB.ID, EndTime: start + int64(time.Hour/time.Millisecond)}
		trip := tripEntity{TripID: tripID{Type: kind, Number: number}, TrainTypeID: train.ID, RouteID: routeID, StartingTime: start, StartingStationID: stationA.ID, StationsID: stationB.ID, TerminalStationID: stationB.ID, EndTime: tripIn.EndTime}
		records[index] = record{
			config:   configEntity{Name: token + "Config", Value: token + "-v0", Description: "benchmark " + token},
			stationA: stationA, stationB: stationB, train: train,
			routeIn: routeIn, route: route, price: price, tripIn: tripIn, trip: trip,
		}
	}
	return records
}

func deterministicUUID(rng *rand.Rand) string {
	var raw [16]byte
	for index := range raw {
		raw[index] = byte(rng.Intn(256))
	}
	raw[6] = (raw[6] & 0x0f) | 0x40
	raw[8] = (raw[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		binary.BigEndian.Uint32(raw[0:4]),
		binary.BigEndian.Uint16(raw[4:6]),
		binary.BigEndian.Uint16(raw[6:8]),
		binary.BigEndian.Uint16(raw[8:10]),
		raw[10:16],
	)
}

func makeAdminToken(now time.Time) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	claims, _ := json.Marshal(map[string]any{
		"sub": "vibesys-benchmark", "roles": []string{"ROLE_ADMIN"},
		"id": "vibesys-benchmark", "iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	})
	payload := base64.RawURLEncoding.EncodeToString(claims)
	input := header + "." + payload
	mac := hmac.New(sha256.New, []byte("secret"))
	_, _ = mac.Write([]byte(input))
	return input + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
