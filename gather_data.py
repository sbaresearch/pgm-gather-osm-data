import json

from osm_cleaner.clean_gens import clean_gens
from osm_cleaner.clean_lines import clean_lines
from osm_cleaner.clean_subs import clean_subs
from osm_loader.generator_fetcher import get_raw_gens
from osm_loader.line_fetcher import get_raw_lines
from osm_loader.substation_fetcher import get_raw_subs
from util import config
from util.utils import vprint


def gather_data(date: str):
    if config.FETCH_NEW:
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
    line_gdf.to_file(f"{config.RESULTS_DIR}/lines.gpkg", driver="GPKG")

    # ensure correct dtypes for sub fields
    sub_gdf["lat"], sub_gdf["lon"] = sub_gdf.lat.astype(float), sub_gdf.lon.astype(float)
    sub_gdf["voltage"] = sub_gdf["voltage"].apply(lambda lst: [int(x) for x in lst])
    sub_gdf["voltage"] = sub_gdf["voltage"].apply(json.dumps)
    sub_gdf.to_file(f"{config.RESULTS_DIR}/substations.gpkg", driver="GPKG")

    gens_gdf.to_file(f"{config.RESULTS_DIR}/generators.gpkg", driver="GPKG")

    if config.VISUALIZATION:
        from visualization import add_visualization

        vprint("Adding color to line, substation, and generator data")
        line_gdf, sub_gdf, gens_gdf = add_visualization(line_gdf, sub_gdf, gens_gdf)

        line_gdf.to_file(f"{config.RESULTS_DIR}/visualisation/lines.geojson", driver="GeoJSON")
        sub_gdf.to_file(f"{config.RESULTS_DIR}/visualisation/substations.geojson", driver="GeoJSON")
        gens_gdf.to_file(f"{config.RESULTS_DIR}/visualisation/generators.geojson", driver="GeoJSON")
