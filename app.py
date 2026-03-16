from flask import Flask, render_template, jsonify
import ee
import geemap
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
 
app = Flask(__name__)
 
# ── Earth Engine init ──────────────────────────────────────────────────────────
# FIX 1: Use ee.Initialize directly — Authenticate() is only needed once
# interactively. Remove it from production startup to avoid blocking the server.
try:
    ee.Initialize(project='pivotal-mode-459101-a1')
    print("Earth Engine connected")
except Exception as e:
    print("EE init failed:", e)
    raise
 
# ── Load asset ────────────────────────────────────────────────────────────────
fields_ee = ee.FeatureCollection("users/amanbhatt/alathur_paddy_health_latest")
print("Asset loaded")
 
# ── Latest Sentinel-1 acquisition date ───────────────────────────────────────
try:
    latest_s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(fields_ee.geometry())
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .sort("system:time_start", False)
        .first()
    )
    latest_timestamp = latest_s1.get("system:time_start").getInfo()
    latest_date = datetime.fromtimestamp(latest_timestamp / 1000).strftime("%d %b %Y")
except Exception as e:
    print("Warning: could not fetch latest Sentinel-1 date:", e)
    latest_date = "N/A"
 
print("Latest Sentinel-1 acquisition:", latest_date)
 
# ── Convert to GeoJSON ────────────────────────────────────────────────────────
geojson = geemap.ee_to_geojson(fields_ee)
print("Number of fields:", len(geojson["features"]))
 
# ── Assign easy field names AND field_id ─────────────────────────────────────
# FIX 2: field_id must be stored in properties so the frontend timeseries
#         fetch (/timeseries/<field_id>) resolves to the correct feature.
for i, feature in enumerate(geojson["features"], start=1):
    feature["properties"]["field_name"] = f"Field {i}"
    feature["properties"]["field_id"]   = i          # ← was missing before
 
print("\nField Health Data:\n")
 
for feature in geojson["features"]:
    props    = feature["properties"]
    field_name = props.get("field_name")
    vh       = props.get("VH_value")
    baseline = props.get("baseline_VH")
    delta    = props.get("delta_VH")
    health   = props.get("health_status")
    stage    = props.get("crop_stage")
 
    print(
        "Field:", field_name,
        "| VH:", round(vh, 2),
        "| Baseline:", round(baseline, 2),
        "| Delta VH:", round(delta, 2),
        "| Stage:", stage,
        "| Health:", health
    )
 
 
# ── Routes ───
 
@app.route("/")
def dashboard():
    fields   = geojson["features"]
    total    = len(fields)
    healthy  = sum(1 for f in fields if f["properties"]["health_status"] == "Healthy")
    moderate = sum(1 for f in fields if f["properties"]["health_status"] == "Moderate Stress")
    low      = sum(1 for f in fields if f["properties"]["health_status"] == "Low Biomass")
 
    return render_template(
        "index.html",
        data=fields,
        geojson=geojson,
        total_fields=total,
        healthy_fields=healthy,
        moderate_fields=moderate,
        low_fields=low,
        latest_date=latest_date       # ← was passed but never shown before FIX 1 in HTML
    )
 
 
@app.route("/data")
def data():
    return jsonify(geojson)
 
 
@app.route("/timeseries/<int:field_id>")
def timeseries(field_id):
    try:
        # field_id is 1-based (matches the field_name "Field N")
        feature_index = field_id - 1
 
        if feature_index < 0 or feature_index >= len(geojson["features"]):
            return jsonify({"error": "field_id out of range", "features": []}), 404
 
        field_feature = geojson["features"][feature_index]
        coords        = field_feature["geometry"]["coordinates"]
        field_geom    = ee.Geometry.Polygon(coords)
 
        today = datetime.today().strftime("%Y-%m-%d")
 
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(field_geom)
            .filterDate("2023-01-01", today)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .select("VH")
            .sort("system:time_start")
        )
 
        def extract(image):
            vh = image.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=field_geom,
                scale=10,
                bestEffort=True
            ).get("VH")
 
            return ee.Feature(None, {
                "date": image.date().format("YYYY-MM-dd"),
                "VH":   vh
            })
 
        ts   = s1.map(extract)
        data = ts.getInfo()
 
        return jsonify(data)
 
    except Exception as e:
        print("Timeseries error for field_id", field_id, ":", e)
        return jsonify({"features": []}), 500
 
 
from apscheduler.schedulers.background import BackgroundScheduler

def refresh_data():
    global geojson, latest_date

    print("Checking for new Sentinel-1 data...")

    try:
        latest_s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(fields_ee.geometry())
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .sort("system:time_start", False)
            .first()
        )

        latest_timestamp = latest_s1.get("system:time_start").getInfo()
        new_date = datetime.fromtimestamp(latest_timestamp / 1000).strftime("%d %b %Y")

        if new_date != latest_date:
            print("New data found for:", new_date, "— refreshing...")
            latest_date = new_date

            geojson = geemap.ee_to_geojson(fields_ee)

            for i, feature in enumerate(geojson["features"], start=1):
                feature["properties"]["field_name"] = f"Field {i}"
                feature["properties"]["field_id"]   = i

            print("Data refreshed successfully!")

        else:
            print("No new data yet. Latest is still:", latest_date)

    except Exception as e:
        print("Auto-refresh error:", e)


if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data, 'interval', days=6)
    scheduler.start()
    print("Auto-refresh scheduler started — checks every 6 days")

    try:
        app.run(debug=False)
    except KeyboardInterrupt:
        scheduler.shutdown()