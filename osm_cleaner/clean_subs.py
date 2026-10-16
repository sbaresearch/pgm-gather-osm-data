import json

import geopandas as gpd
import pandas as pd
from shapely import MultiPoint, Polygon

from osm_loader.substation_fetcher import get_raw_subs
from util.utils import _clamp_voltage, crs, split_on_multiple, vprint


def clean_subs(date: str) -> gpd.GeoDataFrame:
    """
    Loads substations gotten from OSM before filtering and and cleaning these.

    Returns:
        gdf (GeoDataframe): A GeoDataFrame with all relevant information for the substations.
    """
    subs_way = _get_way_subs(date)
    subs_rel = _get_rel_subs(date)

    gdf = gpd.GeoDataFrame(pd.concat([subs_way, subs_rel]), geometry="polygon", crs=crs).reset_index(drop=True)

    vprint("\tMerging substations intersecting each other")
    gdf = _merge_touching_substation(gdf)  # pyright: ignore[reportArgumentType]

    return gdf


def _get_way_subs(date: str) -> pd.DataFrame:
    try:
        with open(f"osm_data/substation_ways_{date}.json") as f:
            data = json.load(f)
    except FileNotFoundError:
        answer = ""
        while answer != "y":
            answer = input(f"Could not find substation data for date {date}. Fetch new data? (y/n)").lower()
            if answer == "n":
                vprint("Not gathering required data. Aborting...")
                from sys import exit

                exit()

        get_raw_subs(date)

        with open(f"osm_data/substation_ways_{date}.json") as f:
            data = json.load(f)

    df = pd.DataFrame(data["elements"])

    df = _basic_process(df)

    df["polygon"] = df.geometry.apply(_get_polygon)  # type: ignore

    df = _add_geometry_fields(df)

    df = df.drop(columns=["bounds", "geometry", "tags", "nodes"]).reset_index(drop=True)

    return df


def _get_rel_subs(date: str) -> pd.DataFrame:
    with open(f"osm_data/substation_relations_{date}.json") as f:
        data = json.load(f)

    df = pd.DataFrame(data["elements"])

    df = _basic_process(df)

    df["polygon"] = df.members.apply(_get_relation_polygon)  # type: ignore

    df = _add_geometry_fields(df)

    df = df.drop(columns=["bounds", "members", "tags"]).reset_index(drop=True)

    return df


def _basic_process(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df = _filter_low_voltage_subs(df)

    df = _remove_oebb_subs(df)

    df["name"] = df.tags.apply(lambda x: x.get("name"))

    return df


def _add_geometry_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["point"] = df.polygon.apply(lambda x: x.centroid)
    df["lat"] = df.point.apply(lambda x: x.y)
    df["lon"] = df.point.apply(lambda x: x.x)

    return df.drop(columns="point")


def _filter_low_voltage_subs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["voltage"] = df.tags.apply(_get_voltage)
    df["voltage"] = df.voltage.apply(_remove_low_voltage)
    # Remove low voltage levels from voltage list and then remove subs that no longer have any voltage levels listed
    # NOTE: keeps substations that do not have any listed voltage level (with voltage=None)
    subs_to_keep = df.voltage.apply(lambda x: bool(len(x)) if x is not None else True)
    df = df[subs_to_keep]  # pyright: ignore[reportAssignmentType]
    df["voltage"] = df.voltage.apply(_clamp_voltage)

    return df


def _remove_oebb_subs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["operator"] = df.tags.apply(lambda x: x.get("operator"))

    # Remove substations operated by ÖBB
    öbb_subs = df.operator.fillna("").str.lower().str.contains("öbb")
    return df[~öbb_subs]  # pyright: ignore[reportReturnType]


def _get_voltage(tags: dict) -> list[int] | None:
    if "voltage" not in tags:
        return None
    voltage_levels = split_on_multiple(tags["voltage"], [";", ":"])

    try:
        voltage_levels = [int(v) for v in voltage_levels]
    except ValueError:
        return None

    if len(voltage_levels) == 0:
        return None

    return voltage_levels


def _remove_low_voltage(voltage: list[int] | None, threshold=110000) -> list[int] | None:
    if voltage is None:
        return None

    voltage = [v for v in voltage if v >= threshold]

    return voltage


def _get_polygon(geom: list[dict[str, float]]) -> Polygon:
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


def _get_relation_polygon(members: list[dict]) -> Polygon:
    geometries = [member["geometry"] for member in members if member["type"] == "way"]

    point_coords = [(coord["lon"], coord["lat"]) for geom in geometries for coord in geom]

    return MultiPoint(point_coords).convex_hull  # type: ignore


def _merge_touching_substation(subs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Merges substations whose polygons intersect.

    Parameters
    ----------
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.

    Returns
    -------
    - subs (GeoDataFrame): The substation gdf with the intersecting substations merged.
    """
    from shapely import unary_union

    combined_polygons = unary_union(subs.polygon)

    gdf_combined = gpd.GeoDataFrame(geometry=[poly for poly in combined_polygons.geoms], crs=crs)  # type: ignore

    for i, combined_geom in gdf_combined.iterrows():
        mask = subs.intersects(combined_geom.geometry)
        subs.loc[mask, "polygon_merged"] = combined_geom.geometry

    # Find substations whose merged polygons are the same (meaning they should be merged)
    intersecting_subs = subs[subs.polygon_merged.duplicated()]
    new_rows = pd.DataFrame(columns=subs.columns)
    for geom in intersecting_subs.polygon_merged.unique():
        subs_to_merge = subs[subs.polygon_merged == geom]
        new_rows = pd.concat([new_rows, _merge_subs(subs_to_merge)], ignore_index=True)  # pyright: ignore[reportArgumentType]

        subs.drop(index=subs_to_merge.index, inplace=True)

    merged_subs = pd.concat([subs, new_rows], ignore_index=True)
    merged_subs.drop(columns=["polygon"], inplace=True)
    merged_subs.rename(columns={"polygon_merged": "polygon"}, inplace=True)
    merged_subs["id"] = merged_subs.id.astype(int)

    return gpd.GeoDataFrame(merged_subs, crs=crs, geometry="polygon")


def _merge_subs(subs_to_merge: pd.DataFrame) -> pd.DataFrame:
    """
    Merges given substations into a df with one row representing the merged substation. Takes the
    majority of information from the substation with the highest voltage level.

    Parameters
    ----------
    - subs_to_merge (DataFrame): Each row is a substation, and all rows should be merged into one.

    Returns
    -------
    - merged_sub (DataFrame): A df with one row representing the merged substation.

    Notes
    -----
    - Takes the id from the substation with the highest voltage (meaning the other ids are lost).
    """
    subs_to_merge = subs_to_merge.sort_values("voltage", ascending=False)

    # Combine prioritizing information from the sub with the highest voltage.
    merged_sub = subs_to_merge.iloc[0].combine_first(subs_to_merge.iloc[1])
    for i in range(2, len(subs_to_merge)):
        merged_sub.combine_first(subs_to_merge.iloc[i])

    merged_sub["type"] = "-".join([str(type) for type in subs_to_merge["type"].unique()])

    # Take voltage from all substations to merge
    voltages_list = subs_to_merge.voltage.to_list()
    merged_sub["voltage"] = list({v for voltages in voltages_list if voltages is not None for v in voltages})

    return pd.DataFrame(merged_sub).T
