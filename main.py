import json
from datetime import datetime

from osm_cleaner.clean_gens import clean_gens
from osm_cleaner.clean_lines import clean_lines
from osm_cleaner.clean_subs import clean_subs
from osm_loader.generator_fetcher import get_raw_gens
from osm_loader.line_fetcher import get_raw_lines
from osm_loader.substation_fetcher import get_raw_subs
from util.utils import vprint

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Gather substation, line, and generator data from OpenStreetMap and clean and process this data."
    )

    parser.add_argument(
        "-fn", "--fetch_new", action="store_true", help="Fetch new data from OSM. WARNING: should only be done once!"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Prints extra information about the process.")
    parser.add_argument(
        "-d",
        "--date",
        type=str,
        help="Specify a date from which to gather attic data. Format YYYY-MM-DDTHH:MM.\n"
        + 'Gathers data from OSM available at the set date. Leave empty, or use "NAN" to get current data.'
        + "Default: NAN",
        default="NAN",
    )
    parser.add_argument("-vis", "--visualization", action="store_true", help="Create GeoJSON files for visualization.")

    args = parser.parse_args()

    from util import config

    config.verbose = args.verbose

    # Translate date to correct format
    date = "" if args.date == "NAN" else args.date
    if date:
        d = datetime.fromisoformat(date)
        date = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"

    if args.fetch_new:
        vprint("Gathering line data from OSM")
        get_raw_lines(date)
        vprint("Gathering generator data from OSM")
        get_raw_gens(date)
        vprint("Gathering substation data from OSM")
        get_raw_subs(date)

    vprint("Cleaning generator data")
    gens_gdf = clean_gens(date)

    vprint("Cleaning substation data")
    sub_gdf = clean_subs(date)

    vprint("Cleaning line data")
    line_gdf, sub_gdf = clean_lines(sub_gdf, date)

    vprint("Exporting data to results")
    line_gdf.to_file("results/lines.gpkg", driver="GPKG")

    # ensure correct dtypes for sub fields
    sub_gdf["lat"], sub_gdf["lon"] = sub_gdf.lat.astype(float), sub_gdf.lon.astype(float)
    sub_gdf["voltage"] = sub_gdf["voltage"].apply(lambda lst: [int(x) for x in lst])
    sub_gdf["voltage"] = sub_gdf["voltage"].apply(json.dumps)
    sub_gdf.to_file("results/substations.gpkg", driver="GPKG")

    gens_gdf.to_file("results/generators.gpkg", driver="GPKG")

    if args.visualization:
        from visualization import *

        vprint("Adding color to line, substation, and generator data")
        line_gdf = add_line_color(line_gdf)
        line_gdf = shift_lines(line_gdf)
        sub_gdf = add_sub_color(sub_gdf)
        gens_gdf = add_gen_color(gens_gdf)
        gens_gdf = change_gen_unit(gens_gdf)

        line_gdf.to_file("results/visualisation/lines.geojson", driver="GeoJSON")
        sub_gdf.to_file("results/visualisation/substations.geojson", driver="GeoJSON")
        gens_gdf.to_file("results/visualisation/generators.geojson", driver="GeoJSON")
