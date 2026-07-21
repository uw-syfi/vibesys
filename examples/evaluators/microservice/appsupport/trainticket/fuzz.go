// Package trainticket owns mode-neutral Train Ticket input grammars.
package trainticket

import (
	"crypto/hmac"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"time"
)

func Token(random *rand.Rand, namespace string, index int) string {
	return fmt.Sprintf("%s%03x%08x", namespace, index, random.Uint32())
}

func StationName(random *rand.Rand, terminal bool, token string) string {
	prefixes := []string{"Station A ", "North Hub ", "北站 "}
	if terminal {
		prefixes = []string{"Station B ", "South Hub ", "南站 "}
	}
	return prefixes[random.Intn(len(prefixes))] + token
}

func StationStayTime(random *rand.Rand) int   { return 1 + random.Intn(40) }
func TrainEconomyClass(random *rand.Rand) int { return 100 + random.Intn(800) }
func TrainConfortClass(random *rand.Rand) int { return 50 + random.Intn(250) }
func TrainAverageSpeed(random *rand.Rand) int { return 80 + random.Intn(270) }
func RouteDistance(random *rand.Rand) int     { return 100 + random.Intn(1700) }

func UUID(random *rand.Rand) string {
	var raw [16]byte
	for index := range raw {
		raw[index] = byte(random.Intn(256))
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

func PriceRates(random *rand.Rand) (float64, float64) {
	return round4(0.1 + random.Float64()*0.8), round4(0.9 + random.Float64())
}

func TripIdentity(random *rand.Rand) (string, string) {
	kind := "G"
	if random.Intn(2) == 0 {
		kind = "D"
	}
	return kind, fmt.Sprintf("%07d", 1_000_000+random.Intn(9_000_000))
}

func TripTimes(random *rand.Rand) (int64, int64) {
	start := int64(1_600_000_000_000) + random.Int63n(300_000_000_001)
	return start, start + int64(3_600_000+random.Intn(39_600_001))
}

func ConfigName(random *rand.Rand, token string) string {
	values := []string{token + "Config", "config " + token, "配置-" + token}
	return values[random.Intn(len(values))]
}

func UpdatedStationName(version uint64, terminal bool) string {
	prefixes := []string{"Renamed A ", "Transfer Hub A ", "换乘站甲 "}
	if terminal {
		prefixes = []string{"Renamed B ", "Terminal Hub B ", "终点站乙 "}
	}
	return fmt.Sprintf("%s%016x", prefixes[version%uint64(len(prefixes))], version)
}

func UpdatedStationStayTime(version uint64) int            { return 41 + int(version%50) }
func UpdatedTrainSpeed(version uint64) int                 { return 351 + int(version%150) }
func UpdatedTrainEconomy(current int) int                  { return current + 7 }
func UpdatedRouteDistance(current int, version uint64) int { return current + 1 + int(version%99) }

func UpdatedPriceRates(version uint64) (float64, float64) {
	return round4(0.11 + float64(version%7800)/10_000),
		round4(0.91 + float64((version>>16)%9800)/10_000)
}

func UpdatedTripEnd(current int64, version uint64) int64 {
	return current + int64(60_000+version%3_540_001)
}

func AdminToken(now time.Time) (string, error) {
	identityBytes := make([]byte, 12)
	if _, err := cryptorand.Read(identityBytes); err != nil {
		return "", fmt.Errorf("generate Train Ticket identity: %w", err)
	}
	identity := hex.EncodeToString(identityBytes)
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	claimsRaw, err := json.Marshal(map[string]any{
		"sub": identity, "roles": []string{"ROLE_ADMIN"}, "id": identity,
		"iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	})
	if err != nil {
		return "", fmt.Errorf("encode Train Ticket identity: %w", err)
	}
	claims := base64.RawURLEncoding.EncodeToString(claimsRaw)
	input := header + "." + claims
	signer := hmac.New(sha256.New, []byte("secret"))
	_, _ = signer.Write([]byte(input))
	return input + "." + base64.RawURLEncoding.EncodeToString(signer.Sum(nil)), nil
}

func round4(value float64) float64 {
	return math.Round(value*10_000) / 10_000
}
