#!/usr/bin/env python3
"""Probe the Connected Cars GraphQL API for the new (EV) telemetry fields.

Zero dependencies (stdlib only). Run it against your own account to confirm the
fields added to the integration actually return data for your car(s):

    python3 tools/probe_ev_fields.py --email you@example.com --namespace minvolkswagen

You'll be prompted for the password (or set CC_PASSWORD in the environment).
Namespace is the same value used by the integration's config flow:
minvolkswagen (default), minskoda, minseat, mitaudi.
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

AUTH_URL = "https://auth-api.connectedcars.io/auth/login/email/password"
GRAPH_URL = "https://api.connectedcars.io/graphql"

# Mirrors exactly the new fields added to the integration's vehicle query.
QUERY = """query Probe {
  viewer {
    vehicles {
      vehicle {
        id
        name
        make
        model
        fuelType
        isCharging
        chargingState { enabled time }
        chargingStatus {
          startChargePercentage startTime endedAt chargedPercentage
          averageChargeSpeed chargeInKwhIncrease rangeIncrease
          timeUntil80PercentCharge showSummaryForChargeEnded
        }
        chargePercentage { pct time }
        factoryBatteryCapacity { usableCapacityKwh }
        highVoltageBatteryUsableCapacityKwh { kwh time }
        averageBatteryConsumptionInKwhPer100Km { date efficiencyKwhPer100Km }
        adblueRemainingKm(limit: 1) { km }
        driverScore { driverScore previousDriverScore }
        trips(last: 1, ignoreEmpty: true) { items {
          startTime endTime duration idleTime mileage
          startAddressString endAddressString
          startOdometer endOdometer
          fuelUsed electricityUsed
          accelerationHigh accelerationMedium accelerationLow
          brakeHigh brakeMedium brakeLow
          turnHigh turnMedium turnLow
          tripType note
          profilings { type time gForce }
        } }
      }
    }
  }
}"""


def _post(url, body, headers):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        print(f"HTTP {err.code} from {url}:")
        try:
            print(json.dumps(json.loads(raw), indent=2))
        except ValueError:
            print(raw)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    # Same values as the integration's config flow: minvolkswagen (default),
    # minskoda, minseat, mitaudi.
    ap.add_argument("--namespace", default="minvolkswagen")
    args = ap.parse_args()

    password = os.environ.get("CC_PASSWORD") or getpass.getpass("Password: ")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-organization-namespace": f"semler:{args.namespace}",
        "User-Agent": "ConnectedCars/360 CFNetwork/978.0.7 Darwin/18.7.0",
    }

    auth = _post(AUTH_URL, {"email": args.email, "password": password}, headers)
    if "token" not in auth:
        print("Auth failed:", auth)
        sys.exit(1)
    headers["Authorization"] = f"Bearer {auth['token']}"

    result = _post(GRAPH_URL, {"query": QUERY}, headers)
    if "errors" in result:
        print("!! GraphQL returned errors (a field name may be wrong):")
        print(json.dumps(result["errors"], indent=2))
        sys.exit(2)

    viewer = (result.get("data") or {}).get("viewer") or {}
    vehicles = viewer.get("vehicles") or []
    print(f"vehicles returned: {len(vehicles)}")
    if not vehicles:
        print("Raw response:")
        print(json.dumps(result, indent=2)[:4000])
        return
    for item in vehicles:
        v = item["vehicle"]
        print(f"\n=== {v.get('make')} {v.get('model')} ({v.get('fuelType')}) ===")
        for key in [
            "isCharging",
            "chargingState",
            "chargingStatus",
            "chargePercentage",
            "factoryBatteryCapacity",
            "highVoltageBatteryUsableCapacityKwh",
            "averageBatteryConsumptionInKwhPer100Km",
            "adblueRemainingKm",
            "driverScore",
        ]:
            print(f"  {key}: {json.dumps(v.get(key))}")
        trip = ((v.get("trips") or {}).get("items") or [None])[0]
        print("  latest trip:")
        if trip is None:
            print("    (none)")
        else:
            profilings = trip.pop("profilings", None) or []
            for key, value in trip.items():
                print(f"    {key}: {json.dumps(value)}")
            print(f"    profilings ({len(profilings)} events):")
            for event in profilings:
                print(f"      {json.dumps(event)}")


if __name__ == "__main__":
    main()
