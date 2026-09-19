package hotel

import (
	"fmt"
	"math"
	"slices"
	"strconv"
	"time"
)

// hotelState is the complete logical state used by the Hotel reference
// behavior. It deliberately excludes implementation details such as service
// topology, databases, and caches.
type hotelState struct {
	Reserved map[string]int `json:"reserved"`
}

// hotelOracle defines the canonical externally observable behavior of the
// pinned Hotel Reservation application. The generic accuracy framework owns
// event scheduling and comparison; this type owns only Hotel semantics.
type hotelOracle struct {
	catalog map[string]profile
	users   map[string]string
}

func newHotelOracle(catalog map[string]profile) (*hotelOracle, error) {
	users := make(map[string]string, 501)
	for index := 0; index <= 500; index++ {
		username, password, err := User(index)
		if err != nil {
			return nil, err
		}
		users[username] = password
	}
	return &hotelOracle{catalog: catalog, users: users}, nil
}

func (*hotelOracle) Initial() (hotelState, error) {
	return hotelState{Reserved: make(map[string]int)}, nil
}

func (o *hotelOracle) Step(
	state hotelState,
	action differentialAction,
) (hotelState, differentialObservation, error) {
	if state.Reserved == nil {
		state.Reserved = make(map[string]int)
	}
	var observation differentialObservation
	var err error
	switch action.Kind {
	case actionLogin:
		observation = o.login(action.Query)
	case actionRecommend:
		observation, err = o.recommend(action.Query)
	case actionReserve:
		observation, err = o.reserve(state, action.Query)
	case actionSearch:
		observation, err = o.search(state, action.Query)
	default:
		err = fmt.Errorf("unknown Hotel differential action %q", action.Kind)
	}
	return state, observation, err
}

// AfterCrash states Hotel's durability contract: every acknowledged
// reservation remains part of the logical state after a crash.
func (*hotelOracle) AfterCrash(state hotelState) (hotelState, error) { return state, nil }

func (*hotelOracle) Equal(expected, actual differentialObservation) bool {
	return expected.Message == actual.Message && slices.Equal(expected.HotelIDs, actual.HotelIDs)
}

// sequentialModel is a stateful facade used by focused tests and fake
// candidates. All semantics delegate to hotelOracle, the single reference
// behavior used by event programs.
type sequentialModel struct {
	oracle *hotelOracle
	state  hotelState
}

func newSequentialModel(catalog map[string]profile) (*sequentialModel, error) {
	oracle, err := newHotelOracle(catalog)
	if err != nil {
		return nil, err
	}
	state, err := oracle.Initial()
	if err != nil {
		return nil, err
	}
	return &sequentialModel{oracle: oracle, state: state}, nil
}

func (m *sequentialModel) apply(action differentialAction) (differentialObservation, error) {
	next, observation, err := m.oracle.Step(m.state, action)
	m.state = next
	return observation, err
}

func (m *sequentialModel) login(query map[string]string) differentialObservation {
	return m.oracle.login(query)
}

func (m *sequentialModel) search(query map[string]string) (differentialObservation, error) {
	return m.oracle.search(m.state, query)
}

func (m *sequentialModel) validCredentials(query map[string]string) bool {
	return m.oracle.validCredentials(query)
}

func (o *hotelOracle) login(query map[string]string) differentialObservation {
	message := loginFailure
	if o.validCredentials(query) {
		message = loginSuccess
	}
	return messageObservation(message)
}

func (o *hotelOracle) recommend(query map[string]string) (differentialObservation, error) {
	switch query["require"] {
	case "price":
		return hotelIDsObservation("2"), nil
	case "rate":
		return hotelIDsObservation("9", "24", "39", "54", "69"), nil
	case "dis":
		lat, err := strconv.ParseFloat(query["lat"], 64)
		if err != nil {
			return differentialObservation{}, fmt.Errorf("parse recommendation latitude: %w", err)
		}
		lon, err := strconv.ParseFloat(query["lon"], 64)
		if err != nil {
			return differentialObservation{}, fmt.Errorf("parse recommendation longitude: %w", err)
		}
		nearest := ""
		nearestDistance := math.MaxFloat64
		for id, item := range o.catalog {
			distance := math.Hypot(item.recommendLat-lat, item.recommendLon-lon)
			if distance < nearestDistance {
				nearest = id
				nearestDistance = distance
			}
		}
		if nearest == "" {
			return differentialObservation{}, fmt.Errorf("Hotel catalog is empty")
		}
		return hotelIDsObservation(nearest), nil
	default:
		return differentialObservation{}, fmt.Errorf(
			"unsupported recommendation requirement %q", query["require"],
		)
	}
}

func (o *hotelOracle) reserve(state hotelState, query map[string]string) (differentialObservation, error) {
	hotelID := query["hotelId"]
	nights, err := reservationNights(query)
	if err != nil {
		return differentialObservation{}, err
	}
	rooms := 0
	if raw := query["number"]; raw != "" {
		rooms, err = strconv.Atoi(raw)
		if err != nil {
			return differentialObservation{}, fmt.Errorf("parse room count %q: %w", raw, err)
		}
	}
	capacity, err := capacityForHotel(hotelID)
	if err != nil {
		return differentialObservation{}, err
	}
	for _, date := range nights {
		if state.Reserved[reservationStateKey(hotelID, date)]+rooms > capacity {
			return messageObservation(reservationFailure), nil
		}
	}
	for _, date := range nights {
		state.Reserved[reservationStateKey(hotelID, date)] += rooms
	}
	if !o.validCredentials(query) {
		// The pinned frontend invokes MakeReservation even after authentication
		// fails, so a failed-login response can still consume room capacity.
		return messageObservation(loginFailure), nil
	}
	return messageObservation(reservationSuccess), nil
}

func (o *hotelOracle) search(state hotelState, query map[string]string) (differentialObservation, error) {
	nights, err := reservationNights(query)
	if err != nil {
		return differentialObservation{}, err
	}
	available := make([]string, 0, len(rateBackedIDs()))
	for _, hotelID := range sortedRateBackedIDs() {
		capacity, err := capacityForHotel(hotelID)
		if err != nil {
			return differentialObservation{}, err
		}
		isAvailable := true
		for _, date := range nights {
			if state.Reserved[reservationStateKey(hotelID, date)]+1 > capacity {
				isAvailable = false
				break
			}
		}
		if isAvailable {
			available = append(available, hotelID)
		}
	}
	return hotelIDsObservation(available...), nil
}

func reservationStateKey(hotelID, date string) string { return hotelID + ":" + date }

func (o *hotelOracle) validCredentials(query map[string]string) bool {
	return o.users[query["username"]] == query["password"] && query["username"] != ""
}

func reservationNights(query map[string]string) ([]string, error) {
	start, err := time.Parse(time.DateOnly, query["inDate"])
	if err != nil {
		return nil, fmt.Errorf("parse reservation start date %q: %w", query["inDate"], err)
	}
	end, err := time.Parse(time.DateOnly, query["outDate"])
	if err != nil {
		return nil, fmt.Errorf("parse reservation end date %q: %w", query["outDate"], err)
	}
	nights := make([]string, 0)
	for date := start; date.Before(end); date = date.AddDate(0, 0, 1) {
		nights = append(nights, date.Format(time.DateOnly))
	}
	return nights, nil
}

func capacityForHotel(hotelID string) (int, error) {
	numericID, err := strconv.Atoi(hotelID)
	if err != nil {
		return 0, fmt.Errorf("parse hotel ID %q: %w", hotelID, err)
	}
	return Capacity(numericID)
}
