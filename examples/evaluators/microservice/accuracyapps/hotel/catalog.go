package hotel

import (
	"fmt"
	"strconv"
)

type profile struct {
	id, name, phone string
	lat, lon        float32
	recommendLat    float64
	recommendLon    float64
}

var initialProfiles = []profile{
	{id: "1", name: "Clift Hotel", phone: "(415) 775-4700", lat: 37.7867, lon: -122.4112, recommendLat: 37.7867, recommendLon: -122.4112},
	{id: "2", name: "W San Francisco", phone: "(415) 777-5300", lat: 37.7854, lon: -122.4005, recommendLat: 37.7854, recommendLon: -122.4005},
	{id: "3", name: "Hotel Zetta", phone: "(415) 543-8555", lat: 37.7834, lon: -122.4071, recommendLat: 37.7834, recommendLon: -122.4071},
	{id: "4", name: "Hotel Vitale", phone: "(415) 278-3700", lat: 37.7936, lon: -122.3930, recommendLat: 37.7936, recommendLon: -122.3930},
	{id: "5", name: "Phoenix Hotel", phone: "(415) 776-1380", lat: 37.7831, lon: -122.4181, recommendLat: 37.7831, recommendLon: -122.4181},
	{id: "6", name: "St. Regis San Francisco", phone: "(415) 284-4000", lat: 37.7863, lon: -122.4015, recommendLat: 37.7863, recommendLon: -122.4015},
}

// seedProfiles independently reproduces cmd/profile/db.go. In particular, the
// profile service stores generated coordinates as float32 while the
// recommendation service stores its copy as float64.
func seedProfiles() (map[string]profile, error) {
	catalog := make(map[string]profile, 80)
	for _, item := range initialProfiles {
		catalog[item.id] = item
	}
	for id := 7; id <= 80; id++ {
		text := strconv.Itoa(id)
		item := profile{
			id: text, name: "St. Regis San Francisco",
			phone:        fmt.Sprintf("(415) 284-40%s", text),
			lat:          37.7835 + float32(id)/500.0*3,
			lon:          -122.41 + float32(id)/500.0*4,
			recommendLat: 37.7835 + float64(id)/500.0*3,
			recommendLon: -122.41 + float64(id)/500.0*4,
		}
		catalog[text] = item
	}
	if len(catalog) != 80 {
		return nil, fmt.Errorf("Hotel seed catalog has %d profiles, expected 80", len(catalog))
	}
	return catalog, nil
}

func rateBackedIDs() map[string]struct{} {
	result := map[string]struct{}{"1": {}, "2": {}, "3": {}}
	for id := 9; id <= 80; id += 3 {
		result[strconv.Itoa(id)] = struct{}{}
	}
	return result
}
