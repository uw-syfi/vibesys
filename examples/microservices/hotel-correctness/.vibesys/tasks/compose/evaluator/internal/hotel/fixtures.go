package hotel

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
)

func User(index int) (username string, password string, err error) {
	if index < 0 || index > 500 {
		return "", "", fmt.Errorf("Hotel seed user index must be in [0, 500], got %d", index)
	}
	suffix := strconv.Itoa(index)
	return "Cornell_" + hex.EncodeToString([]byte(suffix)), strings.Repeat(suffix, 10), nil
}

func MustUser(index int) (string, string) {
	username, password, err := User(index)
	if err != nil {
		panic(err)
	}
	return username, password
}

func Capacity(hotelID int) (int, error) {
	if hotelID < 1 || hotelID > 80 {
		return 0, fmt.Errorf("Hotel seed hotel ID must be in [1, 80], got %d", hotelID)
	}
	if hotelID <= 6 || hotelID%3 == 0 {
		return 200, nil
	}
	if hotelID%3 == 1 {
		return 300, nil
	}
	return 250, nil
}
