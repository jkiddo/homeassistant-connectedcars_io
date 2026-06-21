# Connected Cars GraphQL API — integration notes

Notes on the Connected Cars backend this integration talks to. The API is
publicly documented and self-describing:

- Official docs: <https://github.com/connectedcars/docs> (`graphql.md`,
  `auth-api.md`, `push-v2.md`, `webhooks.md`, `data-overview/`).
- Interactive explorer + schema introspection:
  <https://api.connectedcars.io/graphql/graphiql/>.

Everything below was confirmed against the live schema (introspection) and a
real vehicle.

## Transport

- **Auth:** `POST https://auth-api.connectedcars.io/auth/login/email/password`
  (regional variants `auth-api.eu1` / `au1` / `sp1`). Returns `{ token, expires }`.
- **Data:** `POST https://api.connectedcars.io/graphql`, `Authorization: Bearer
  <token>`, `x-organization-namespace: semler:<namespace>` where `<namespace>`
  is the brand app: `minvolkswagen`, `minskoda`, `minseat`, `mitaudi`.

## Capabilities

The data source is an **OBD-II telemetry dongle**. The API is **read-only
telemetry**: the mutations on the schema are all account / consent / messaging /
trip-tagging (`GiveConsent`, `TagTrip`, `UpdateUser`, `UpdateVehicleOdometer`,
`DismissLead`, `SnoozeVehicle`, …). There is no lock/unlock, climate, honk/flash,
or charge-control mutation, and **no refresh/wake mutation** — so data freshness
is entirely the dongle's own reporting cadence. A Home Assistant integration can
therefore only be read-only sensors.

## Vehicle fields used (beyond the original integration)

Introspecting the `Vehicle` type returns **202 fields** — far more than any one
app surfaces. Fields newly exposed by this integration, confirmed live on a
Volkswagen ID.4 (electric):

| Field | Shape | Exposed as |
|---|---|---|
| `isCharging` | `Boolean` | **Charging** binary_sensor |
| `chargingStatus` | `VehicleChargeStatus` | **ChargingStatus** sensor (see below) |
| `driverScore` | `{ driverScore, previousDriverScore }` | **DriverScore** sensor (out of ten) |
| `adblueRemainingKm(limit:N)` | `[{ km }]` | **AdBlueRange** sensor (diesel) |
| `highVoltageBatteryHealth` | `{ relativeUsableCapacity, predictedRelativeUsableCapacity, cycles, time }` | **EVBatteryHealth** (`0.870` → 87% SoH) |
| `estimatedUsableBatteryCapacityInKwh` | `{ usableCapacityKwh, date }` | **EVBatteryCapacity** state (`67.79` kWh) |
| `highVoltageBatteryTotalCapacityKwh` | `{ kwh, time }` | EVBatteryCapacity attr (`73`) |
| `factoryBatteryCapacity` | `{ usableCapacityKwh, totalCapacityKwh }` | EVBatteryCapacity attrs (`77` / `82`) |
| `highVoltageBatteryUsableCapacityKwh` | `{ kwh, time }` | EVBatteryCapacity "energy now" attr (`44`) |
| `averageBatteryConsumptionInKwhPer100Km` | `{ efficiencyKwhPer100Km, date }` | **EVEfficiency** sensor (`25.1`) |
| `batteryEfficiencyKmPerKwh` | `Float` | EVEfficiency km/kWh attr (`3.98`) |
| `isMainPowerDisconnected` | `Boolean` | **PowerDisconnected** binary_sensor |

Battery degradation is internally consistent: `44 kWh ≈ 66% SoC × 67.79 kWh`
current usable capacity, i.e. the pack is at **87%** of its 77 kWh usable spec.

Fields that returned null on the test EV (so not exposed): `fuelTankSize`,
`avgCO2EmissionKm`, `fuelEconomyLiter100Km`, `roadworthyInspectionDate`,
`userUsableBatteryCapacityInKwh`. The push/stream API (`push-v2.md`) and
`webhooks.md` only carry vehicle lifecycle events (activated/connected) plus VIN
— no telemetry.

### `chargingStatus` (type `VehicleChargeStatus`)

```
startChargePercentage    # SoC % when the session started
startTime                # ISO timestamp
endedAt                  # ISO timestamp, null while charging
chargedPercentage        # % points gained this session
averageChargeSpeed       # treated as kW (used as the sensor state)
chargeInKwhIncrease       # kWh added
rangeIncrease            # km added
timeUntil80PercentCharge # time remaining to 80%
showSummaryForChargeEnded
```

While the car is idle these are all null (they populate during a charge
session), so the ChargingStatus sensor is gated on the always-present
`isCharging` signal — it exists for EVs and goes available during charging.

*Unit assumptions:* `averageChargeSpeed` is treated as kW (POWER device class),
`rangeIncrease` as km, `chargeInKwhIncrease` as kWh — not explicitly labelled in
the schema; adjust if a live charge session reports otherwise.

## Testing against a real account

`tools/probe_ev_fields.py` (stdlib only) runs the same auth + query the
integration uses and prints the fields per vehicle, so you can confirm field
names resolve and see which return data for your car:

```
python3 tools/probe_ev_fields.py --email you@example.com --namespace minvolkswagen
```
