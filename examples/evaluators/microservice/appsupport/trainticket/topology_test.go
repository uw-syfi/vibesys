package trainticket

import "testing"

func TestTopologyOwnsAllServicePrefixesAndCollections(t *testing.T) {
	want := map[string]string{
		"config": "/api/v1/configservice/configs", "station": "/api/v1/stationservice/stations",
		"train": "/api/v1/trainservice/trains", "travel": "/api/v1/travelservice/trips",
		"route": "/api/v1/routeservice/routes", "price": "/api/v1/priceservice/prices",
	}
	for _, service := range Services() {
		if got := MustCollectionPath(service); got != want[service] {
			t.Fatalf("%s collection=%q, want %q", service, got, want[service])
		}
	}
	if _, err := Path("unknown", "/objects"); err == nil {
		t.Fatal("unknown service was accepted")
	}
	if _, err := Path("config", "configs"); err == nil {
		t.Fatal("relative path without leading slash was accepted")
	}
}
