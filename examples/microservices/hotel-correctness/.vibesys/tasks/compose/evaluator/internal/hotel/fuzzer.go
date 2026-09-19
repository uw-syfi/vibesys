package hotel

import (
	"context"
	"fmt"
	"math/rand"
	"sort"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/accuracy"
)

type differentialActionKind string

const (
	actionLogin     differentialActionKind = "login"
	actionRecommend differentialActionKind = "recommend"
	actionReserve   differentialActionKind = "reserve"
	actionSearch    differentialActionKind = "search"
)

type differentialAction struct {
	Kind  differentialActionKind `json:"kind"`
	Query map[string]string      `json:"query"`
}

type differentialObservation struct {
	Message  string   `json:"message,omitempty"`
	HotelIDs []string `json:"hotel_ids,omitempty"`
}

func (a *Application) verifySequentialDifferential(
	ctx context.Context,
	c client,
	seed int64,
	cases int,
) (int, error) {
	program, err := generateSequentialProgram(seed, cases, a.catalog)
	if err != nil {
		return 0, err
	}
	return a.verifyHotelProgram(ctx, c, program, nil, nil)
}

func (a *Application) observeDifferentialAction(
	ctx context.Context,
	c client,
	action differentialAction,
) (differentialObservation, error) {
	switch action.Kind {
	case actionLogin:
		message, err := c.message(ctx, "/user", action.Query)
		if err != nil {
			return differentialObservation{}, err
		}
		return messageObservation(message), nil
	case actionReserve:
		message, err := c.message(ctx, "/reservation", action.Query)
		if err != nil {
			return differentialObservation{}, err
		}
		return messageObservation(message), nil
	case actionRecommend, actionSearch:
		path := "/recommendations"
		if action.Kind == actionSearch {
			path = "/hotels"
		}
		features, err := c.geoJSON(ctx, path, action.Query)
		if err != nil {
			return differentialObservation{}, err
		}
		if err := validateProfiles(features, a.catalog, "sequential differential "+string(action.Kind)); err != nil {
			return differentialObservation{}, err
		}
		ids := make([]string, 0, len(features))
		for id := range features {
			ids = append(ids, id)
		}
		sortHotelIDs(ids)
		return hotelIDsObservation(ids...), nil
	default:
		return differentialObservation{}, fmt.Errorf("unknown Hotel differential action %q", action.Kind)
	}
}

func generateSequentialProgram(
	seed int64,
	cases int,
	catalog map[string]profile,
) (accuracy.Program[differentialAction], error) {
	if cases < 1 {
		return accuracy.Program[differentialAction]{}, fmt.Errorf(
			"Hotel differential cases must be positive, got %d", cases,
		)
	}
	if len(catalog) != 80 {
		return accuracy.Program[differentialAction]{}, fmt.Errorf(
			"Hotel differential catalog has %d profiles, expected 80", len(catalog),
		)
	}
	random := rand.New(rand.NewSource(seed ^ 0x4d6f64656c))
	history := make([]differentialAction, 0, 5+cases*9)

	userIndex := random.Intn(501)
	username, password, err := User(userIndex)
	if err != nil {
		return accuracy.Program[differentialAction]{}, err
	}
	history = append(history,
		differentialAction{Kind: actionLogin, Query: credentials(username, password)},
		differentialAction{Kind: actionLogin, Query: credentials(username, password+"-wrong")},
		differentialAction{Kind: actionRecommend, Query: recommendationQuery("price", catalog["2"], false)},
		differentialAction{Kind: actionRecommend, Query: recommendationQuery("rate", catalog["9"], true)},
	)
	distanceID := strconv.Itoa(1 + random.Intn(80))
	history = append(history, differentialAction{
		Kind:  actionRecommend,
		Query: recommendationQuery("dis", catalog[distanceID], random.Intn(2) == 0),
	})

	rateIDs := sortedRateBackedIDs()
	random.Shuffle(len(rateIDs), func(left, right int) {
		rateIDs[left], rateIDs[right] = rateIDs[right], rateIDs[left]
	})
	start := reservationDate(seed).AddDate(0, 0, 1100)
	for caseIndex := 0; caseIndex < cases; caseIndex++ {
		primary := rateIDs[caseIndex%len(rateIDs)]
		isolation := rateIDs[(caseIndex+1)%len(rateIDs)]
		caseStart := start.AddDate(0, 0, caseIndex*4)
		primaryDates := [2]string{
			caseStart.Format(time.DateOnly),
			caseStart.AddDate(0, 0, 1).Format(time.DateOnly),
		}
		authDates := [2]string{
			caseStart.AddDate(0, 0, 2).Format(time.DateOnly),
			caseStart.AddDate(0, 0, 3).Format(time.DateOnly),
		}
		capacity, err := capacityForHotel(primary)
		if err != nil {
			return accuracy.Program[differentialAction]{}, err
		}
		firstRooms := 1 + random.Intn(capacity-1)
		secondRooms := capacity - firstRooms
		validUser, validPassword, err := User(random.Intn(501))
		if err != nil {
			return accuracy.Program[differentialAction]{}, err
		}

		history = append(history,
			differentialAction{Kind: actionSearch, Query: searchQuery(primaryDates, catalog[primary], caseIndex%2 == 0)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				primary, primaryDates, firstRooms, fmt.Sprintf("differential-%d-first", caseIndex), validUser, validPassword,
			)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				primary, primaryDates, secondRooms, fmt.Sprintf("differential-%d-fill", caseIndex), validUser, validPassword,
			)},
			differentialAction{Kind: actionSearch, Query: searchQuery(primaryDates, catalog[primary], caseIndex%2 != 0)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				primary, primaryDates, 1, fmt.Sprintf("differential-%d-over", caseIndex), validUser, validPassword,
			)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				primary, authDates, capacity, fmt.Sprintf("differential-%d-invalid-auth", caseIndex), validUser, validPassword+"-wrong",
			)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				primary, authDates, 1, fmt.Sprintf("differential-%d-auth-readback", caseIndex), validUser, validPassword,
			)},
			differentialAction{Kind: actionSearch, Query: searchQuery(authDates, catalog[primary], true)},
			differentialAction{Kind: actionReserve, Query: reservationActionQuery(
				isolation, authDates, 1, fmt.Sprintf("differential-%d-isolation", caseIndex), validUser, validPassword,
			)},
		)
	}
	steps := make([]accuracy.Step[differentialAction], 0, len(history))
	for index, action := range history {
		steps = append(steps, callStep(fmt.Sprintf("sequential-%03d", index), action))
	}
	return accuracy.Program[differentialAction]{
		SchemaVersion: accuracy.ProgramSchemaVersion,
		ID:            "hotel-sequential-differential",
		Steps:         steps,
	}, nil
}

func recommendationQuery(require string, item profile, locale bool) map[string]string {
	query := map[string]string{
		"require": require,
		"lat":     strconv.FormatFloat(item.recommendLat, 'f', -1, 64),
		"lon":     strconv.FormatFloat(item.recommendLon, 'f', -1, 64),
	}
	if locale {
		query["locale"] = "en"
	}
	return query
}

func searchQuery(dates [2]string, item profile, locale bool) map[string]string {
	query := map[string]string{
		"inDate": dates[0], "outDate": dates[1],
		"lat": strconv.FormatFloat(item.recommendLat, 'f', -1, 64),
		"lon": strconv.FormatFloat(item.recommendLon, 'f', -1, 64),
	}
	if locale {
		query["locale"] = "en"
	}
	return query
}

func reservationActionQuery(
	hotelID string,
	dates [2]string,
	rooms int,
	customerName string,
	username string,
	password string,
) map[string]string {
	query := credentials(username, password)
	query["hotelId"] = hotelID
	query["inDate"] = dates[0]
	query["outDate"] = dates[1]
	query["number"] = strconv.Itoa(rooms)
	query["customerName"] = customerName
	return query
}

func messageObservation(message string) differentialObservation {
	return differentialObservation{Message: message}
}

func hotelIDsObservation(ids ...string) differentialObservation {
	result := append([]string(nil), ids...)
	sortHotelIDs(result)
	return differentialObservation{HotelIDs: result}
}

func sortHotelIDs(ids []string) {
	sort.Slice(ids, func(left, right int) bool {
		leftID, leftErr := strconv.Atoi(ids[left])
		rightID, rightErr := strconv.Atoi(ids[right])
		if leftErr != nil || rightErr != nil {
			return ids[left] < ids[right]
		}
		return leftID < rightID
	})
}
