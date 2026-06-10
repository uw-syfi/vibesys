# Reference Implementation
HTTP server is run on port 8000 to handle the system initialisation. 
Single lock protects all reservation writes to avoid double-booking.

Endpoints

GET  /health
POST /reset                         — wipe all state (used by checker / benchmark)
POST /hotels                        — create hotel
GET  /hotels                        — list all hotels
GET  /hotels/{hotel_id}             — get hotel
GET  /hotels/{hotel_id}/availability?check_in=YYYY-MM-DD&check_out=YYYY-MM-DD
POST /users                         — create user
POST /reservations                  — place reservation
GET  /reservations/{reservation_id} — get reservation
DELETE /reservations/{reservation_id} — cancel reservation


To run, execute the following:

```bash
pip install fastapi uvicorn
python reference.py
```

Server starts at `http://localhost:8000`. Hit `/health` to confirm it's up.
