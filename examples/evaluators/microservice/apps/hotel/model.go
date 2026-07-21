package hotel

import (
	"fmt"
	"strconv"
)

type operationState struct {
	kind        string
	expectedIDs []string
	release     func()
}

type profile struct {
	id           string
	name         string
	phoneNumber  string
	lat          float32
	lon          float32
	recommendLat float64
	recommendLon float64
}

var fixedProfiles = map[string]profile{
	"1": {id: "1", name: "Clift Hotel", phoneNumber: "(415) 775-4700", lat: 37.7867, lon: -122.4112, recommendLat: 37.7867, recommendLon: -122.4112},
	"2": {id: "2", name: "W San Francisco", phoneNumber: "(415) 777-5300", lat: 37.7854, lon: -122.4005, recommendLat: 37.7854, recommendLon: -122.4005},
	"3": {id: "3", name: "Hotel Zetta", phoneNumber: "(415) 543-8555", lat: 37.7834, lon: -122.4071, recommendLat: 37.7834, recommendLon: -122.4071},
	"4": {id: "4", name: "Hotel Vitale", phoneNumber: "(415) 278-3700", lat: 37.7936, lon: -122.3930, recommendLat: 37.7936, recommendLon: -122.3930},
	"5": {id: "5", name: "Phoenix Hotel", phoneNumber: "(415) 776-1380", lat: 37.7831, lon: -122.4181, recommendLat: 37.7831, recommendLon: -122.4181},
	"6": {id: "6", name: "St. Regis San Francisco", phoneNumber: "(415) 284-4000", lat: 37.7863, lon: -122.4015, recommendLat: 37.7863, recommendLon: -122.4015},
}

func profileForID(id string) (profile, error) {
	if item, ok := fixedProfiles[id]; ok {
		return item, nil
	}
	number, err := strconv.Atoi(id)
	if err != nil || number < 7 || number > 80 || strconv.Itoa(number) != id {
		return profile{}, fmt.Errorf("unknown Hotel profile ID %q", id)
	}
	return profile{
		id:           id,
		name:         "St. Regis San Francisco",
		phoneNumber:  "(415) 284-40" + id,
		lat:          37.7835 + float32(number)/500.0*3,
		lon:          -122.41 + float32(number)/500.0*4,
		recommendLat: 37.7835 + float64(number)/500.0*3,
		recommendLon: -122.41 + float64(number)/500.0*4,
	}, nil
}

func rateBackedHotelIDs() []string {
	ids := []string{"1", "2", "3"}
	for id := 9; id <= 78; id += 3 {
		ids = append(ids, strconv.Itoa(id))
	}
	return ids
}
