#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scenario_dir="$(cd "${script_dir}/.." && pwd)"
source_dir="${scenario_dir}/../social-network-read-timeline/3rd_party/deathstarbench/hotelReservation"
destination="${scenario_dir}/hotelReservation"

if [[ ! -f "${source_dir}/docker-compose.yml" ]]; then
  echo "DeathStarBench is not initialized at ${source_dir}" >&2
  echo "Run: git submodule update --init examples/microservices/social-network-read-timeline/3rd_party/deathstarbench" >&2
  exit 1
fi
if [[ -e "${destination}" ]]; then
  echo "Reference already exists at ${destination}; remove it explicitly before refreshing." >&2
  exit 1
fi

mkdir -p "$(dirname "${destination}")"
cp -a "${source_dir}" "${destination}"
rm -f "${destination}/.git"
echo "Materialized Hotel Reservation candidate at ${destination}"
