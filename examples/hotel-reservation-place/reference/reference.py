from __future__ import annotations

import threading
import uuid
from datetime import date, timedelta
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field



# Single reentrant lock

_lock = threading.RLock()

_hotels: Dict[str, dict] = {}
_users: Dict[str, dict] = {}
_reservations: Dict[str, dict] = {}


class HotelCreate(BaseModel):
    name: str
    location: str
    star_rating: int = Field(ge=1, le=5)
    rooms: List[RoomSpec]


class RoomSpec(BaseModel):
    room_type: str
    capacity: int = Field(ge=1)
    rate_per_night: float = Field(gt=0)


class HotelCreate(BaseModel):
    name: str
    location: str
    star_rating: int = Field(ge=1, le=5)
    rooms: List[RoomSpec]


class UserCreate(BaseModel):
    name: str
    email: str


class ReservationRequest(BaseModel):
    user_id: str
    hotel_id: str
    room_type: str
    check_in: date
    check_out: date


class ReservationResponse(BaseModel):
    reservation_id: str
    user_id: str
    hotel_id: str
    room_id: str
    room_type: str
    check_in: date
    check_out: date
    total_price: float
    status: str  # "confirmed" OR "cancelled"



# Helpers


def _date_range(check_in: date, check_out: date) -> List[date]:
    nights = (check_out - check_in).days
    if nights <= 0:
        raise ValueError("check_out must be after check_in")
    return [check_in + timedelta(days=i) for i in range(nights)]


def _room_is_available(room_id: str, check_in: date, check_out: date) -> bool:
    nights = set(_date_range(check_in, check_out))
    for res in _reservations.values():
        if res["status"] != "confirmed":
            continue
        if res["room_id"] != room_id:
            continue
        existing_nights = set(_date_range(res["check_in"], res["check_out"]))
        if nights & existing_nights:
            return False
    return True


def _find_available_room(hotel_id: str, room_type: str, check_in: date, check_out: date) -> Optional[dict]:
    hotel = _hotels.get(hotel_id)
    if not hotel:
        return None
    for room in hotel["rooms"]:
        if room["room_type"] != room_type:
            continue
        if _room_is_available(room["room_id"], check_in, check_out):
            return room
    return None



# FastAPI

app = FastAPI(title="Hotel Reservation Reference")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reset", status_code=204)
def reset():
    with _lock:
        _hotels.clear()
        _users.clear()
        _reservations.clear()


@app.post("/hotels", status_code=201)
def create_hotel(body: HotelCreate):
    hotel_id = str(uuid.uuid4())
    rooms_with_ids = []
    for room in body.rooms:
        rooms_with_ids.append({
            "room_id": str(uuid.uuid4()),
            "room_type": room.room_type,
            "capacity": room.capacity,
            "rate_per_night": room.rate_per_night,
        })
    hotel = {
        "hotel_id": hotel_id,
        "name": body.name,
        "location": body.location,
        "star_rating": body.star_rating,
        "rooms": rooms_with_ids,
    }
    with _lock:
        _hotels[hotel_id] = hotel
    return hotel


@app.get("/hotels")
def list_hotels():
    with _lock:
        return list(_hotels.values())


@app.get("/hotels/{hotel_id}")
def get_hotel(hotel_id: str):
    with _lock:
        hotel = _hotels.get(hotel_id)
    if not hotel:
        raise HTTPException(status_code=404, detail="Hotel not found")
    return hotel


@app.get("/hotels/{hotel_id}/availability")
def get_availability(hotel_id: str, check_in: date, check_out: date):
    with _lock:
        hotel = _hotels.get(hotel_id)
        if not hotel:
            raise HTTPException(status_code=404, detail="Hotel not found")
        availability: Dict[str, int] = {}
        for room in hotel["rooms"]:
            rt = room["room_type"]
            if _room_is_available(room["room_id"], check_in, check_out):
                availability[rt] = availability.get(rt, 0) + 1
    return {
        "hotel_id": hotel_id,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "available_rooms": availability,
    }


@app.post("/users", status_code=201)
def create_user(body: UserCreate):
    user_id = str(uuid.uuid4())
    user = {"user_id": user_id, "name": body.name, "email": body.email}
    with _lock:
        _users[user_id] = user
    return user


@app.post("/reservations", status_code=201)
def place_reservation(body: ReservationRequest):
    with _lock:
        if body.user_id not in _users:
            raise HTTPException(status_code=404, detail="User not found")
        hotel = _hotels.get(body.hotel_id)
        if not hotel:
            raise HTTPException(status_code=404, detail="Hotel not found")
        try:
            nights = _date_range(body.check_in, body.check_out)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        room = _find_available_room(body.hotel_id, body.room_type, body.check_in, body.check_out)
        if room is None:
            raise HTTPException(
                status_code=409,
                detail=f"No available {body.room_type} rooms for the requested dates",
            )
        reservation_id = str(uuid.uuid4())
        total_price = room["rate_per_night"] * len(nights)
        reservation = {
            "reservation_id": reservation_id,
            "user_id": body.user_id,
            "hotel_id": body.hotel_id,
            "room_id": room["room_id"],
            "room_type": body.room_type,
            "check_in": body.check_in,
            "check_out": body.check_out,
            "total_price": total_price,
            "status": "confirmed",
        }
        _reservations[reservation_id] = reservation
    return reservation


@app.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: str):
    with _lock:
        res = _reservations.get(reservation_id)
    if not res:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return res


@app.delete("/reservations/{reservation_id}", status_code=200)
def cancel_reservation(reservation_id: str):
    with _lock:
        res = _reservations.get(reservation_id)
        if not res:
            raise HTTPException(status_code=404, detail="Reservation not found")
        if res["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Reservation already cancelled")
        res["status"] = "cancelled"
    return res

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
