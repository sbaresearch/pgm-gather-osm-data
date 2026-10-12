import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from osm_loader.generator_fetcher import get_raw_gens
from util.utils import crs, vprint


def clean_gens(date: str) -> gpd.GeoDataFrame:
    """
    Loads generators gotten from OSM before filtering and and cleaning these.

    Returns:
        gdf (GeoDataframe): A GeoDataFrame with all relevant information for the generators.
    """
    # Handle ways
    vprint("\tProcessing way generators")

    try:
        with open(f"osm_data/generator_ways_{date}.json") as f:
            way_data = json.load(f)
    except FileNotFoundError:
        answer = ""
        while answer != "y":
            answer = input(f"Could not find generator data for date {date}. Fetch new data? (y/n)").lower()
            if answer == "n":
                vprint("Not gathering required data. Aborting...")
                from sys import exit

                exit()

        get_raw_gens(date)

        with open(f"osm_data/generator_ways_{date}.json") as f:
            way_data = json.load(f)

    way_df = pd.DataFrame(way_data["elements"])

    way_df["output_w"] = way_df.tags.apply(_get_output)
    way_df = way_df.dropna().reset_index(drop=True)

    way_df["polygon"] = way_df.geometry.apply(_get_polygon_ways)  # type: ignore

    way_df.drop(columns=["geometry", "nodes"], inplace=True)

    # Handle relations
    vprint("\tProcessing relation generators")

    with open("osm_data/generator_relations.json") as f:
        rel_data = json.load(f)

    rel_df = pd.DataFrame(rel_data["elements"])

    rel_df["output_w"] = rel_df.tags.apply(_get_output)
    rel_df = rel_df.dropna().reset_index(drop=True)

    is_in_ways = rel_df.members.apply(lambda x: any(member["ref"] in way_df.id.values for member in x))
    rel_df = rel_df[~is_in_ways].copy()

    rel_df["polygon"] = rel_df.bounds.apply(_get_polygon_relations)  # type: ignore

    rel_df.drop(columns=["members"], inplace=True)

    # Merge and batch process
    df = pd.concat([way_df, rel_df]).reset_index(drop=True)

    df["point"] = df.polygon.apply(lambda x: x.centroid)
    df["lat"] = df.point.apply(lambda x: x.y)
    df["lon"] = df.point.apply(lambda x: x.x)

    df["operator"] = df.tags.apply(lambda x: x.get("operator"))
    df["name"] = df.tags.apply(lambda x: x.get("name"))
    df["source"] = df.tags.apply(lambda x: x.get("plant:source"))
    df["method"] = df.tags.apply(lambda x: x.get("plant:method"))

    df = df.drop(columns=["bounds", "tags", "point"]).reset_index(drop=True)

    gdf = gpd.GeoDataFrame(df, geometry="polygon", crs=crs)

    return gdf


def _get_output(tags: dict, threshold: float = 1e6) -> float | None:
    """
    Reads the output value from a tags dict and returns the value in watt.
    Returns None if no value can be read, or if the value is below the threshold.

    Parameters
    ----------
        tags (dict): A dict, hopefully containing the key "plant:output:electricity".

    Returns
    -------
        output_w (float|None): The output value in watt, as a float. If there is no value, or it is below
        the threshold, None is returned.
    """
    if "plant:output:electricity" not in tags:
        return None

    gen = tags["plant:output:electricity"].lower()
    gen = gen.replace(",", ".")
    gen = gen.split(" ")

    if len(gen) == 1:
        gen = gen[0]
        if "kw" in gen:
            idx = gen.find("kw")
        elif "mw" in gen:
            idx = gen.find("mw")
        else:
            return None

        value, unit = gen[:idx], gen[idx:]
    elif len(gen) == 2:
        value, unit = gen[0], gen[1]
    else:
        return None

    try:
        value = float(value)
    except TypeError:
        return None

    factor: float
    if "kw" in unit:
        factor = 1e3
    elif "mw" in unit:
        factor = 1e6
    else:
        return None

    output_w = value * factor

    return output_w if output_w >= threshold else None


def _get_polygon_ways(geom: list[dict[str, float]]) -> Polygon:
    """
    Create a Shapely Polygon from a list of coordinate dictionaries. TAKEN FROM pypsa-eur

    Parameters
    ----------
        geom (list): List of dictionaries with 'lat' and 'lon' keys
        representing coordinates.

    Returns
    -------
        shapely.geometry.Polygon: The constructed polygon object.
    """
    # Extract coordinates as tuples
    point_coords = [(coord["lon"], coord["lat"]) for coord in geom]

    # Ensure closure by repeating the first coordinate as the last coordinate
    if point_coords[0] != point_coords[-1]:
        point_coords.append(point_coords[0])

    # Create Polygon object
    polygon = Polygon(point_coords)

    return polygon


def _get_polygon_relations(bounds: dict[str, float]) -> Polygon:
    """
    Create a Shapely Polygon from a dictionary with boundary points.

    Parameters
    ----------
        bounds (dict): A dictionary with four points.

    Returns
    -------
        shapely.geometry.Polygon: The constructed polygon object.
    """
    x1, x2 = bounds["minlon"], bounds["maxlon"]
    y1, y2 = bounds["minlat"], bounds["maxlat"]

    points = [(x1, y1), (x1, y2), (x2, y2), (x2, y1), (x1, y1)]

    # Create Polygon object
    polygon = Polygon(points)

    return polygon
