from flask import Flask, render_template, jsonify
import ee
import json 
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
 
app = Flask(__name__)
 
# ── Earth Engine init ──────────────────────────────────────────────────────────
# FIX 1: Use ee.Initialize directly — Authenticate() is only needed once
# interactively. Remove it from production startup to avoid blocking the server.
import os
import json

creds_json = os.environ.get("EE_CREDENTIALS")
if creds_json:
    creds_path = os.path.expanduser("~/.config/earthengine/credentials")
    os.makedirs(os.path.dirname(creds_path), exist_ok=True)
    with open(creds_path, "w") as f:
        f.write(creds_json)

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
import os, json

CACHE_FILE = "/tmp/geojson_cache.json"

if os.path.exists(CACHE_FILE):
    print("Loading from cache...")
    with open(CACHE_FILE, "r") as f:
        geojson = json.load(f)
else:
    geojson = fields_ee.getInfo()
    geojson["type"] = "FeatureCollection"
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
@app.route("/refresh")
def manual_refresh():
    recalculate_health()
    return jsonify({"status": "done", "latest_date": latest_date})
 
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
def recalculate_health():
    global geojson, latest_date

    print("Recalculating field health from latest Sentinel-1 data...")

    try:
        paddy_fields = ee.FeatureCollection("users/amanbhatt/alathur_paddy_fields")

        # Assign field_id
        paddy_fields = paddy_fields.map(
            lambda f: f.set('field_id', f.id())
        )

        # Latest Sentinel-1 image
        s1 = (
            ee.ImageCollection('COPERNICUS/S1_GRD')
            .filterBounds(paddy_fields)
            .filterDate('2023-01-01', datetime.today().strftime("%Y-%m-%d"))
            .filter(ee.Filter.eq('instrumentMode', 'IW'))
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
            .select('VH')
        )

        # Speckle filter
        def speckle_filter(image):
            vh = image.select('VH').focal_median(30, 'circle', 'meters')
            return image.addBands(vh.rename('VH'), None, True) \
                        .copyProperties(image, ['system:time_start'])

        s1_filtered = s1.map(speckle_filter)

        # Latest image
        latest_image = s1_filtered.sort('system:time_start', False).first()
        latest_timestamp = latest_image.get('system:time_start').getInfo()
        new_date = datetime.fromtimestamp(latest_timestamp / 1000).strftime("%d %b %Y")

        # Current VH per field
        current_fields = latest_image.select('VH').reduceRegions(
            collection=paddy_fields,
            reducer=ee.Reducer.mean(),
            scale=10
        )

        # Crop stage classification
        def classify_stage(feature):
            vh = ee.Number(feature.get('mean'))
            stage = ee.Algorithms.If(
                vh.lt(-22), 'Flooded / Sowing',
                ee.Algorithms.If(
                    vh.lt(-18), 'Early Growth',
                    ee.Algorithms.If(
                        vh.lt(-15), 'Vegetative Growth',
                        'Peak Biomass'
                    )
                )
            )
            return feature.set({'VH_value': vh, 'crop_stage': stage})

        classified_fields = current_fields.map(classify_stage)

        # Seasonal baseline (previous 3 years, same day ±7)
        latest_date_ee = ee.Date(latest_image.get('system:time_start'))
        latest_year    = latest_date_ee.get('year')
        doy            = latest_date_ee.getRelative('day', 'year')

        start_year = ee.Number(latest_year).subtract(3)
        end_year   = ee.Number(latest_year).subtract(1)

        historical = s1.filter(ee.Filter.calendarRange(start_year, end_year, 'year'))

        seasonal_collection = historical.filter(
            ee.Filter.calendarRange(
                ee.Number(doy).subtract(7),
                ee.Number(doy).add(7),
                'day_of_year'
            )
        )

        baseline_seasonal = seasonal_collection.select('VH') \
            .median() \
            .reduceRegions(
                collection=paddy_fields,
                reducer=ee.Reducer.mean(),
                scale=10
            )

        # Health classification
        def classify_health(feature):
            field_id = feature.get('field_id')

            baseline_feature = baseline_seasonal \
                .filter(ee.Filter.eq('field_id', field_id)) \
                .first()

            baseline_vh = ee.Algorithms.If(
                baseline_feature,
                ee.Number(ee.Feature(baseline_feature).get('mean')),
                -18
            )

            current_vh = ee.Number(feature.get('VH_value'))
            delta_vh   = current_vh.subtract(ee.Number(baseline_vh))

            health = ee.Algorithms.If(
                delta_vh.gt(-0.5), 'Healthy',
                ee.Algorithms.If(
                    delta_vh.gt(-2), 'Moderate Stress',
                    'Low Biomass'
                )
            )

            return feature.set({
                'baseline_VH': baseline_vh,
                'delta_VH':    delta_vh,
                'health_status': health
            })

        health_fields = classified_fields.map(classify_health)

        # Add coordinates
        def add_coords(feature):
            centroid = feature.geometry().centroid()
            coords   = centroid.coordinates()
            return feature.set({
                'longitude': coords.get(0),
                'latitude':  coords.get(1)
            })

        export_fields = health_fields.map(add_coords)

        # Export updated asset back to GEE
        task = ee.batch.Export.table.toAsset(
            collection=export_fields,
            description='alathur_paddy_health_latest',
            assetId='users/amanbhatt/alathur_paddy_health_latest'
        )
        task.start()
        print("GEE export task started — asset will update in a few minutes")

        # Update in-memory geojson immediately from fresh calculation
        new_geojson = export_fields.getInfo()
        new_geojson["type"] = "FeatureCollection"

        for i, feature in enumerate(new_geojson["features"], start=1):
            feature["properties"]["field_name"] = f"Field {i}"
            feature["properties"]["field_id"]   = i

        geojson     = new_geojson
        with open(CACHE_FILE, "w") as f:
         json.dump(geojson, f)
        print("Cache saved!")
        latest_date = new_date

        print("Health recalculation complete! Latest date:", latest_date)

    except Exception as e:
        print("Recalculation error:", e)

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
    scheduler.add_job(recalculate_health, 'interval', days=6)
    scheduler.start()
    print("Auto-refresh scheduler started — checks every 6 days")

    try:
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port, debug=False)
    except KeyboardInterrupt:
        scheduler.shutdown()