import json

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import snap, split
from sklearn.cluster import DBSCAN
from tqdm import tqdm

from osm_loader.line_fetcher import get_raw_lines
from util.utils import _clamp_voltage, crs, distance_crs, split_on_multiple, tabbed_tqdm_format, vprint


def clean_lines(
    sub_gdf: gpd.GeoDataFrame, date: str, voltage_threshold=110000
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Loads lines fetched from OSM before filtering and and cleaning these.

    Returns:
        gdf (GeoDataFrame): A GeoDataFrame with all relevant information for the lines
        sub (GeoDataFrame): A GeoDataFrame with all relevant information for the subs. Gets updated based on the line,
        with things such as virtual substations.
    """
    with open(f"osm_data/lines_{date}.json") as f:
        data = json.load(f)

    try:
        with open(f"osm_data/lines_{date}.json") as f:
            data = json.load(f)
    except FileNotFoundError:
        answer = ""
        while answer != "y":
            answer = input(f"Could not find line data for date {date}. Fetch new data? (y/n)").lower()
            if answer == "n":
                vprint("Not gathering required data. Aborting...")
                from sys import exit

                exit()

        get_raw_lines(date)

        with open(f"osm_data/lines_{date}.json") as f:
            data = json.load(f)

    df = pd.DataFrame(data["elements"])

    vprint("\tFiltering out low voltage lines")
    # These attributes may NOT be None
    df["voltage"] = df.tags.apply(lambda x: get_voltage(x, voltage_threshold))
    df.dropna(inplace=True)

    # These attributes may be None
    df["operator"] = df.tags.apply(lambda x: x.get("operator"))

    # Remove lines operated by ÖBB
    öbb_lines = df.operator.fillna("").str.lower().str.contains("öbb")
    df = df[~öbb_lines]

    df["name"] = df.tags.apply(lambda x: x.get("name"))
    df["circuits"] = df.tags.apply(lambda x: x.get("circuits")).astype("Int16")
    df["cables"] = df.tags.apply(lambda x: x.get("cables"))

    # TODO: Use ";" cables when splitting systems. Now only the first value is used
    sep_cables = df.cables[df.cables.str.contains(";").astype(bool)]
    df.loc[sep_cables.index, "cables"] = sep_cables.apply(lambda s: s[: s.find(";")])
    df["cables"] = df.cables.astype("Int16")

    df["wires"] = df.tags.apply(lambda x: x.get("wires"))
    df["ref"] = df.tags.apply(lambda x: x.get("ref"))
    df["id"] = df.id.astype(str)  # id can be amended when line gets amended

    vprint("\tCleaning up circuits, cables, and wires")
    # Update circuits from cables if circuits is missing ...
    df["circuits"] = df.apply(
        lambda l: l.cables // 3 if not pd.isna(l.cables) and pd.isna(l.circuits) else l.circuits, axis=1
    )
    # and make sure to always have at least 1 circuit
    df["circuits"] = df.circuits.apply(lambda c: max(c, 1) if not pd.isna(c) else None).astype("Int16")
    df.drop(columns="cables", inplace=True)
    # Some lines are marked as "singles" instead of "single"
    df["wires"] = df.wires.replace({"singles": "single"})

    vprint("\tCreating line geometry")
    df = _create_lines_geometry(df)  # pyright: ignore[reportArgumentType]

    df.drop(columns=["bounds", "tags", "nodes", "type"], inplace=True)

    # Create GeoDataFrame
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=crs)

    vprint("\tRemoving lines fully within substations")
    gdf = _remove_lines_within_substations(gdf, sub_gdf)

    vprint("\tClamping line voltage to 110, 220, or 380kV")
    gdf["voltage"] = gdf.voltage.apply(_clamp_voltage)

    vprint("\tSplit lines per voltage level, wires, and ref")
    gdf = _split_lines_into_systems(gdf)

    # Handle special case where information is missing for viennese lines
    gdf = _update_vienna_circuits(gdf)

    vprint("\tSplitting lines that go over substations")
    gdf = _split_lines_over_subs(gdf, sub_gdf)

    vprint("\tConnecting lines to substation, when end is in a substation polygon")
    gdf = _connect_lines_to_substations(gdf, sub_gdf)

    vprint("\tOverwriting substation voltage from connected line voltage")
    sub_gdf = _update_sub_voltage_from_lines(gdf, sub_gdf)

    vprint("\tAdding virtual buses at line-ends not connected to a real bus")
    gdf, sub_gdf = _add_virtual_buses(gdf, sub_gdf)

    vprint("\tSplitting lines that go over virtual substations")
    virtual_subs = sub_gdf[sub_gdf["type"] == "virtual_substation"].copy()
    gdf = _split_lines_over_subs(gdf, virtual_subs, remove_endpoints=False)

    vprint("\tMerging virtual buses within a set radius of real substations")
    gdf, sub_gdf = _merge_virtual_buses_with_substations(gdf, sub_gdf)

    vprint("\tMerging virtual buses within a set radius of each other")
    gdf, sub_gdf = _merge_virtual_buses_with_each_other(gdf, sub_gdf)

    vprint("\tRemoving substations with no connected lines")

    def has_connected_lines(sub_id):
        connected_lines = gdf[(gdf.substation_1 == sub_id) | (gdf.substation_2 == sub_id)]
        return not connected_lines.empty

    sub_gdf = sub_gdf[sub_gdf.id.apply(has_connected_lines)]

    vprint("\tRemoving lines connecting the same substation")
    gdf, sub_gdf = _remove_shorting_lines(gdf, sub_gdf)

    vprint("\tRemoving virtual buses only connecting two lines and merge these")
    gdf, sub_gdf = _merge_broken_lines(gdf, sub_gdf)

    vprint("\tRemoving virtual buses one step away from substations")
    gdf, sub_gdf = _remove_final_step_virtual_buses(gdf, sub_gdf)

    vprint("\tRemoving lines connecting the same substation")
    gdf, sub_gdf = _remove_shorting_lines(gdf, sub_gdf)

    vprint("\tOverwriting substation voltage from connected line voltage")
    sub_gdf = _update_sub_voltage_from_lines(gdf, sub_gdf)

    vprint("\tCalculating line length")
    gdf["length"] = gdf.to_crs(distance_crs).length

    return gdf, sub_gdf


def get_voltage(tags: dict, threshold: int) -> list[int] | None:
    """
    Safely get voltage level over specified threshold.

    Parameters
    ----------
        tags (dict): dictionary used to find "voltage" tag.
        threshold (int): only returns voltage values above or equal to this value

    Returns
    -------
        list|None: Returns a list of voltage levels if any are found above the specified threshold.
        Returns None if "voltage" is not available in tags, or if no voltage level above the threshold is found.
    """
    if not isinstance(tags, dict) or "voltage" not in tags:
        return None
    voltage_levels = split_on_multiple(tags["voltage"], [";", ":"])

    try:
        voltage_levels = [int(v) for v in voltage_levels if int(v) >= threshold]
    except ValueError:
        return None

    if len(voltage_levels) == 0:
        return None

    return voltage_levels


def _create_lines_geometry(df_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Create line geometry for the given DataFrame of lines. TAKEN FROM pypsa-eur.

    Parameters
    ----------
    - df_lines (pandas.DataFrame): DataFrame containing lines data.

    Returns
    -------
    - df_lines (pandas.DataFrame): DataFrame with transformed 'geometry'
      column (type: shapely LineString).

    Notes
    -----
    - This function transforms 'geometry' column in the input DataFrame by
      applying the '_create_linestring' function to each row.
    - It then drops rows where the geometry has equal start and end points,
      as these are usually not lines but outlines of areas.
    """
    df_lines = df_lines.copy()
    df_lines.loc[:, "geometry"] = df_lines.apply(_create_linestring, axis=1)

    bool_circle = df_lines["geometry"].apply(lambda x: x.coords[0] == x.coords[-1])
    df_lines = df_lines[~bool_circle]

    return df_lines


def _create_linestring(tags: dict):
    """
    Create a LineString object from the given dictionary. TAKEN FROM pypsa-eur.

    Args:
        tags (dict): A dictionary containing the line data. Assumed to have a "geometry" key.

    Returns:
        LineString: A LineString object representing the geometry.
    """
    coords = [(coord["lon"], coord["lat"]) for coord in tags["geometry"]]
    return LineString(coords)


def _remove_lines_within_substations(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Removes lines that are within substation polygons from the given
    GeoDataFrame of lines. These are not needed to create network (e.g. bus
    bars, switchgear, etc.). TAKEN FROM pypsa-eur.

    Parameters
    ----------
    - lines (GeoDataFrame): A GeoDataFrame containing lines with 'line_id'
      and 'geometry' columns.
    - subs (GeoDataFrame): A GeoDataFrame containing
      substation polygons.

    Returns
    -------
    GeoDataFrame (GeoDataFrame): A new GeoDataFrame without lines within substation polygons.
    """
    lines_in_subs = gpd.sjoin(lines[["id", "geometry"]], subs, how="inner", predicate="within")["id_left"]

    lines = lines[~lines["id"].isin(lines_in_subs)]

    return lines


def _split_lines_into_systems(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Split lines based on the ref, voltage, and wires columns. Explodes over these columns, so that
    these columns do not contain any lists. Also divides the cables and circuits to match the new rows.

    The cables are always grouped in sets of three. If the cables cannot be evenly divided between rows,
    the first rows get another set of cables.

    If there are more refs than voltage or wires, the first voltage/wires get duplicated.

    For example:
    voltage             wires           ref                 cables  circuits    ->  voltage     wires       ref     cables  circuits
    [380000, 110000]    "double;single" "318;319"           9       2               380000      "double"    "318"   6       1
                                                                                    110000      "single"    "319"   3       1
    [380000]            "double"        "410;411"           12      2               380000      "double"    "410"   6       1
                                                                                    380000      "double"    "411"   6       1
    [380000, 220000]    "double;single" "122/A;122/B;123"   9       3               380000      "double"    "122/A" 3       1
                                                                                    380000      "double"    "122/B" 3       1
                                                                                    220000      "single"    "123"   3       1

    Parameters
    ----------
    - lines (GeoDataFrame): A GeoDataFrame containing lines with 'voltage', 'wires', 'ref',
      'cable', and 'circuit' columns.

    Returns
    ----------
    - lines (GeoDataFrame): The updated GeoDataFrame

    Notes
    -----
    - Ignores the circuits value, and overwrites it based on the calculated cables / 3.
    - Always groups cables into at sets of 3, and gives each line a minimum of one set.
    - Fails if there are rows which have more than one voltage level, and wires category, and
    these do not match (throws error).
    - Fails if there are rows with more than one ref value, but less than the number of voltage
    or wires (throws error).
    """
    lines = lines.copy()

    lines["wires"] = lines.wires.apply(lambda l: l.split(";") if not pd.isna(l) else [None])

    from util.utils import split_on_multiple

    def split_ref(ref):
        if ref == "":
            return [None]

        split_ref = split_on_multiple(ref, [";"])
        split_ref = [s for s in split_ref if s]  # Remove missing values
        return split_ref

    lines["ref"] = lines.ref.fillna("").apply(split_ref)

    len_voltage = lines.voltage.apply(len)
    len_wires = lines.wires.apply(len)
    len_ref = lines.ref.apply(len)

    if any((len_voltage > 1) & (len_wires > 1) & (len_wires != len_voltage)):
        raise ValueError("Voltage and wires length mismatch")

    # Number of rows to split line into
    n = np.maximum(len_voltage, len_wires)
    n = np.maximum(n, len_ref)

    # Match voltage, ref, and wires length
    # Lines now has as many voltage, ref, and wires values, excpet in some cases, where:
    # len_ref>1 and len_voltage>1, but len_ref!=len_voltage (or wires instead of voltage)
    lines = lines.assign(
        voltage=lines.voltage.where(len_voltage > 1, lines.voltage * n),
        wires=lines.wires.where(len_wires > 1, lines.wires * n),
        ref=lines.ref.where(len_ref > 1, lines.ref * n),
        _n=n,
    )

    if ((lines.ref.apply(len) > 1) & (lines.voltage.apply(len) > lines.ref.apply(len))).any():
        raise ValueError("Ref and (wires or voltage) lenght mismatch!")

    # Handle cases where there are more refs than voltage, but still more than one voltage (or wires):
    # Prioritizes early values when stretching. EG:
    # n=4, voltage=[380, 110] -> [380, 380, 110, 110]
    # n=3, voltage=[380, 110] -> [380, 380, 110]
    def stretch_to_n(values, n):
        L = len(values)
        if L == n:
            return values

        base = n // L
        rem = n % L
        return [v for i, v in enumerate(values) for _ in range(base + (i < rem))]

    lines["voltage"] = [stretch_to_n(v, n) for v, n in zip(lines.voltage, lines._n)]
    lines["wires"] = [stretch_to_n(v, n) for v, n in zip(lines.wires, lines._n)]

    # Update circuits based on how many times the row is split
    # Must be at least as many circuits as number of splits.
    lines["circuits"] = lines.apply(
        lambda l: max(l.circuits, l._n) if not pd.isna(l.circuits) else None, axis=1
    ).astype("Int8")
    base = lines.circuits // lines._n
    rem = lines.circuits % lines._n

    # split circuits based on number of lines to split into, giving more circuits to earlier lines if necessary.
    lines["circuits"] = [
        [(b + 1)] * r + [b] * (n - r) if not pd.isna(b) else [None] * n for b, r, n in zip(base, rem, lines._n)
    ]

    def create_unique_id(id, refs):
        L = len(refs)
        if L == 1:
            return [id]

        if len(set(refs)) == L:
            # All unique refs
            return [f"{id}:{ref}" for ref in refs]

        return [f"{id}({i})" for i in range(L)]

    lines["id_new"] = [create_unique_id(id, refs) for id, refs in zip(lines["id"], lines.ref)]

    # Need to transform to pd dataframe to do paired explosion:
    lines_df = pd.DataFrame(lines)
    lines_df = (
        lines_df.explode(["voltage", "circuits", "wires", "id_new", "ref"]).reset_index(drop=True).drop(columns="_n")
    )
    lines_df = lines_df.drop(columns="id").rename(columns={"id_new": "id"})
    lines_df = lines_df.astype({"voltage": "Int64", "circuits": "Int8"})
    lines = gpd.GeoDataFrame(lines_df, geometry=lines.geometry.name, crs=lines.crs)

    return lines


def _update_vienna_circuits(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Give lines in Vienna with no listed circuits 2 circuits (a reasonable guess)"""
    lines = lines.copy()

    def update_circuits(line):
        if not pd.isna(line.circuits):
            return line.circuits
        if not pd.isna(line.operator) and "wien" in line.operator.lower():
            return 2
        return pd.NA

    lines["circuits"] = lines.apply(update_circuits, axis=1).astype("Int8")

    return lines


def _split_lines_over_subs(
    lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame, tol: float = 0.1, remove_endpoints=True
) -> gpd.GeoDataFrame:
    """
    Splits line that go through substations in the middle of the substations

    Parameters
    ----------
    - lines (GeoDataFrame): A GeoDataFrame containing lines.
    - subs (GeoDataFrame): A GeoDataFrame containing substation polygons.

    Returns
    -------
    GeoDataFrame (GeoDataFrame): An updated GeoDataFrame with the lines passing over substations split.

    Notes
    -----
    - Iterates over each row, which is a very slow procedure. Should be vectorized somehow.
    - Does not split short lines (i.e. lines with fewer than 4 segments).
    """
    # Translate to high resolution coordinates
    lines = lines.copy().to_crs(distance_crs)
    subs = subs.copy().to_crs(distance_crs)

    lines_to_remove = []
    new_lines = []
    with tqdm(total=len(lines), bar_format=tabbed_tqdm_format) as pbar:
        for idx, line in lines.iterrows():
            pbar.update(1)

            geom = line.geometry
            # Remove the endpoint, since they are allowed to go through substations
            if len(geom.coords) < 4:
                continue  # don't split lines smaller than 4 segments

            if remove_endpoints:
                geom = LineString(geom.coords[1:-1])

            # Bounding box check
            possible_matches = subs.sindex.intersection(geom.bounds)
            if len(possible_matches) == 0:
                continue

            possible_subs = subs.iloc[possible_matches]

            subs_within_tol = possible_subs[possible_subs.geometry.distance(geom) <= tol]

            subs_within_tol = _endpoints_not_in_sub(geom, subs_within_tol, tol)

            if subs_within_tol.empty:
                continue

            # Part of line inside substation
            inside = geom.intersection(subs_within_tol.geometry)
            assert isinstance(inside, gpd.geoseries.GeoSeries)
            points = inside.interpolate(0.5, normalized=True)

            if len(points) == 1 and not points.iloc[0]:
                # Skip if can't find point of line inside substation
                continue

            lines_to_remove.append(idx)
            split_lines = [line]
            for i, (point, sub_id) in enumerate(zip(points, subs_within_tol.id)):
                if not point:
                    continue
                assert isinstance(point, Point)

                if not (hasattr(line, "substation_1") and hasattr(line, "substation_2")):
                    sub_id = None

                # Snap point to line
                point = geom.interpolate(geom.project(point))

                # Find which line is closest to the point
                line_to_split_idx = np.argmin([l.geometry.distance(point) for l in split_lines])
                line_to_split = split_lines[line_to_split_idx]
                del split_lines[line_to_split_idx]

                split_lines.extend(_split_line_on_points(line_to_split, point, sub_id))

            # Rename lines (does not make sure the lines are numbered in order, i.e. part "2" might end up next to part "0")
            for i, new_line in enumerate(split_lines):
                new_line["name"] = f"{line.name}_{i}"
                new_line["id"] = f"{line.id}_{i}"
            new_lines.extend(split_lines)

    lines.drop(index=lines_to_remove, inplace=True)

    new_line = gpd.GeoDataFrame(new_lines, crs=distance_crs)
    lines = pd.concat([lines, new_line], ignore_index=True)  # type: ignore

    return lines.to_crs(crs)


def _endpoints_not_in_sub(line_geom: LineString, subs: gpd.GeoDataFrame, tol: float) -> gpd.GeoDataFrame:
    """
    Filters the subs gdf to only contain rows where neither of the line endpoints are close to the sub.

    Parameters
    ----------
    - line_geom (LineString): Representing the line.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.
    - tol (float): The maximum distance the line-end and substation can be and still be considered close.

    Returns
    -------
    subs (GeoDataFrame): The original gdf, but with only the rows representing the substations that are not close to an endpoint of the line.
    """
    endpoints = [line_geom.boundary.geoms[0], line_geom.boundary.geoms[1]]

    def endpoint_close_to_sub(polygon_geom):
        return any(polygon_geom.distance(endpoint) < tol for endpoint in endpoints)

    return subs[~subs.geometry.apply(endpoint_close_to_sub)]


def _split_line_on_points(line, point: Point, sub_id: int | None = None) -> list:
    """
    Splits the given line at the point specified.

    Parameters
    ----------
    - line: A representation of the line, with a geometry property of type LineString.
    - point (Point): The point at which the line is split.
    - sub_id (int|None): The id of the sub that the line is going over. Is set as the new connected sub
                    if it is not None.

    Returns
    -------
    new_lines (list): A list containing two copies of the given line, but one with the geometry of the line before the
    given point, and the other one with the geometry after the given point.

    Notes
    -----
    - Shifts the geometry of the given line to make sure the line is on the line (shapely limitation).
    """
    geom = snap(line.geometry, point, 1e-8)
    parts = split(geom, point)

    new_lines = [line.copy(), line.copy()]
    for i, new_line in enumerate(new_lines):
        new_line["geometry"] = parts.geoms[i]

        if sub_id is not None:
            # The line with the first geom ends here (i.e, should have the sub_id for its substation_2)
            new_line[f"substation_{2 - i}"] = sub_id

    return new_lines


def _connect_lines_to_substations(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Connects lines to substations if the line ends inside of a substation

    Parameters
    ----------
    - line_geom (LineString): Representing the line.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.

    Returns
    -------
    lines (GeoDataFrame): The original gdf, but with the extra columns substation_1 and substation_2, containing the
    id of the substation the line ending was inside (or NaN if the line did not end inside of a substation).
    """
    # Translate to high resolution coordinates
    lines = lines.copy().to_crs(distance_crs)
    subs = subs.copy().to_crs(distance_crs)

    with tqdm(total=len(lines) * 2, bar_format=tabbed_tqdm_format) as pbar:
        lines["substation_1"] = lines.apply(lambda l: _get_endpoint_sub(l, subs, 0, pbar), axis=1).astype(
            pd.Int64Dtype()
        )
        lines["substation_2"] = lines.apply(lambda l: _get_endpoint_sub(l, subs, 1, pbar), axis=1).astype(
            pd.Int64Dtype()
        )

    return lines.to_crs(crs)


def _get_endpoint_sub(line, subs: gpd.GeoDataFrame, endpoint_index: int, pbar=None) -> int | None:
    """See _connect_lines_to_substations."""
    geom = line.geometry

    endpoint = geom.boundary.geoms[endpoint_index]  # type: ignore Don't know why pylance doesn't accept this

    assert isinstance(endpoint, Point)

    subs = subs[subs.geometry.contains(endpoint)]

    # NOTE: the end point might be in multiple substations, in which case I just take a random one

    if pbar is not None:
        pbar.update(1)

    return int(subs.iloc[0].id) if not subs.empty else None


def _update_sub_voltage_from_lines(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    subs = subs.copy()

    def extend_sub_voltage(sub):
        connected_lines = lines[(lines.substation_1 == sub.id) | (lines.substation_2 == sub.id)]
        line_voltages = [int(v) for v in connected_lines.voltage.unique()]
        sub_voltages = sub.voltage if sub.voltage is not None else []
        return list(set(line_voltages + sub_voltages))

    subs["voltage"] = subs.apply(extend_sub_voltage, axis=1)
    return subs


def _remove_shorting_lines(
    lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Removes lines that connect the same substation. Then removes virtual substations that are now not connected with any lines"""
    # Remove lines that connect the same substation
    same_sub_connectors = lines[lines.substation_1 == lines.substation_2]
    lines = lines.drop(same_sub_connectors.index).reset_index(drop=True)

    # Find and remove virtual subs with no connected lines
    virtual_subs = subs[subs["type"] == "virtual_substation"]

    def has_connected_lines(vsub):
        return len(lines[(lines.substation_1 == vsub.id) | (lines.substation_2 == vsub.id)]) > 0

    is_isolated = ~virtual_subs.apply(has_connected_lines, axis=1)
    isolated_vsubs = virtual_subs[is_isolated]

    subs = subs.drop(isolated_vsubs.index).reset_index(drop=True)

    return lines, subs


def _cut_lines_at_border(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Clips lines at Austrian border using the clip method. The shapefile is a slightly simplified version of the official Austria region.
    NOTE: Does's not place virtual substations at the end of the lines. If you want to do this: Make sure to already have substations at the end
    of every line, and then check which lines are no longer close to their virtual substation. Remove the virtual substations that are outside of
    Austria, and tag the new ones you create."""
    austria_shape = gpd.read_file("data/Austria/Austria.shp")
    lines = lines.to_crs(distance_crs)
    subs = subs.to_crs(distance_crs)

    print("Clipping lines at border")
    lines = lines.clip(austria_shape)
    print("Done clipping.")

    lines = lines.to_crs(crs)
    subs = subs.to_crs(crs)
    return lines, subs


def _add_virtual_buses(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Adds virtual buses at end of lines not already connected to substation.

    Parameters
    ----------
    - lines (GeoDataFrame): A representation of the line, with a geometry property of type LineString.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.

    Returns
    -------
    - lines (GeoDataFrame): The lines gdf with the new virtual buses added as substation parameters.
    - subs (GeoDataFrame): The substation gdf with the created virtual buses.
    """
    # Create a new gdf with the location of the endpoints of the lines that are not connected to a substation
    endpoint_gdfs = []
    for endpoint in range(1, 3):
        missing_sub = lines[pd.isna(lines[f"substation_{endpoint}"])]
        endpoints = missing_sub[["id", "voltage", "geometry"]].copy().rename(columns={"id": "line_id"})
        endpoints["line_id"] = f"{endpoint}_" + endpoints.line_id.astype(str)
        endpoints["geometry"] = endpoints["geometry"].apply(lambda x, endpoint=endpoint: x.boundary.geoms[endpoint - 1])
        endpoint_gdfs.append(endpoints)
    virtual_subs = gpd.GeoDataFrame(pd.concat(endpoint_gdfs, ignore_index=True), crs=crs, geometry="geometry")

    # Aggregate over geometry (only keep one copy of subs in the same location)
    virtual_subs = (
        virtual_subs.groupby("geometry")
        .agg(
            {
                "line_id": list,  # Combine ids into a list
                "voltage": list,  # Combine voltages into a list
            }
        )
        .reset_index()
    )

    virtual_subs["voltage"] = virtual_subs.voltage.apply(lambda voltages: list(set(voltages)))

    # The id of OSM subs is large, and wont be overwritten here.
    virtual_subs["id"] = virtual_subs.index

    # Connect lines to the appropriate virtual substation
    for vsub in virtual_subs.itertuples():
        for id in vsub.line_id:  # type: ignore
            endpoint, line_id = int(id[0]), id[2:]
            lines.loc[lines.id == line_id, f"substation_{endpoint}"] = vsub.id
    virtual_subs.drop(columns="line_id", inplace=True)

    # Add known substation data
    virtual_subs["type"] = "virtual_substation"
    virtual_subs["lon"] = virtual_subs.geometry.apply(lambda point: point.x)
    virtual_subs["lat"] = virtual_subs.geometry.apply(lambda point: point.y)
    virtual_subs["point"] = virtual_subs.geometry
    buffer = 1e-4

    def poly_from_point(point):
        return Polygon(
            (
                (point.x - buffer, point.y - buffer),
                (point.x + buffer, point.y - buffer),
                (point.x + buffer, point.y + buffer),
                (point.x - buffer, point.y + buffer),
            )
        )

    virtual_subs["polygon"] = virtual_subs.point.apply(poly_from_point)
    virtual_subs.drop(columns=["geometry", "point"], inplace=True)

    # Add "nan" for substation data that is irrelevant for virtual substations
    for col in subs.columns:
        if col not in virtual_subs.columns:
            virtual_subs[col] = np.nan

    subs = gpd.GeoDataFrame(pd.concat([subs, virtual_subs], ignore_index=True), crs=crs, geometry="polygon")
    return lines, subs


def _merge_virtual_buses_with_substations(
    lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame, tol: float = 150
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Merge virtual buses with substations within the tolerance distance. Sometimes lines end close to a substation rather
    than in it, in which case the lines will be connected through this function.

    Parameters
    ----------
    - lines (GeoDataFrame): A representation of the line, with a geometry property of type LineString.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.
    - tol (float): Virtual buses within this distance of a substation polygon will be merged with the substation. Default: 120

    Returns
    -------
    - lines (GeoDataFrame): The lines gdf with updates substation values.
    - subs (GeoDataFrame): The substation gdf without the merged virtual buses.
    """
    lines = lines.copy().to_crs(distance_crs)
    virtual_subs = subs[subs["type"] == "virtual_substation"].copy().to_crs(distance_crs)
    real_subs = subs[subs["type"] != "virtual_substation"].copy().to_crs(distance_crs)

    vsub_to_remove = []

    with tqdm(total=len(virtual_subs), bar_format=tabbed_tqdm_format) as pbar:
        for vsub in virtual_subs.itertuples():
            pbar.update(1)
            # Find closest substation and check if it's within tolerance distance
            closest_subs = real_subs.distance(vsub.polygon.centroid).sort_values()  # type: ignore
            closest_dist, closest_sub_idx = closest_subs.iloc[0], closest_subs.index[0]
            if closest_dist > tol:
                continue

            # Get substation and update voltage level based on line voltage
            sub = real_subs.loc[closest_sub_idx]
            real_subs.at[closest_sub_idx, "voltage"] = list(set(sub.voltage + vsub.voltage))  # type: ignore

            sub_center = sub.polygon.centroid.coords[0]

            # Extend lines to the center of the substation
            lines_starting_here = lines[lines.substation_1 == vsub.id]
            lines.loc[lines_starting_here.index, "geometry"] = lines_starting_here.geometry.apply(
                lambda ls, sub_center=sub_center: LineString([sub_center, *ls.coords[:]])
            )  # type: ignore
            lines.loc[lines_starting_here.index, "substation_1"] = sub.id

            lines_ending_here = lines[lines.substation_2 == vsub.id]
            lines.loc[lines_ending_here.index, "geometry"] = lines_ending_here.geometry.apply(
                lambda ls, sub_center=sub_center: LineString([*ls.coords[:], sub_center])
            )  # type: ignore
            lines.loc[lines_ending_here.index, "substation_2"] = sub.id

            vsub_to_remove.append(vsub.Index)

    # Remove merged virtual buses.
    virtual_subs.drop(index=vsub_to_remove, inplace=True)
    virtual_subs = virtual_subs.to_crs(crs)
    real_subs = real_subs.to_crs(crs)

    subs = gpd.GeoDataFrame(pd.concat([real_subs, virtual_subs], ignore_index=True), crs=crs, geometry="polygon")

    return lines.to_crs(crs), subs


def _merge_virtual_buses_with_each_other(
    lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame, tol: float = 100
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    virtual_subs = subs[subs["type"] == "virtual_substation"].copy()

    # Change geometry to center instead of polygon, and change to hi-fi crs
    virtual_subs["point"] = virtual_subs.polygon.apply(lambda x: x.centroid)
    virtual_subs.set_geometry("point", crs=crs, inplace=True)
    virtual_subs.to_crs(distance_crs, inplace=True)

    # Merge using DBSCAN (clustering)
    coords = np.column_stack((virtual_subs.geometry.x, virtual_subs.geometry.y))
    db = DBSCAN(eps=tol, min_samples=1).fit(coords)
    virtual_subs["cluster_id"] = db.labels_

    def merge_voltage(series):
        return list({v for voltages in series for v in voltages})

    merged = (
        virtual_subs.groupby("cluster_id")
        .agg(
            type=("type", "first"),
            id=("id", "first"),
            operator=("operator", "first"),
            name=("name", "first"),
            point=("point", "first"),
            lat=("lat", "first"),
            lon=("lon", "first"),
            polygon=("polygon", "first"),
            voltage=("voltage", merge_voltage),
        )
        .reset_index(drop=True)
    )
    merged = gpd.GeoDataFrame(merged, geometry="point", crs=distance_crs).to_crs(crs)

    # Connect lines to new vsubs
    vsub_id_to_cluster_id = pd.Series(virtual_subs.cluster_id.values, index=virtual_subs.id).to_dict()
    vsub_id_to_merged_id = {
        vsub_id: int(merged.iloc[cluster_id].id) for vsub_id, cluster_id in vsub_id_to_cluster_id.items()
    }

    with tqdm(total=len(lines), bar_format=tabbed_tqdm_format) as pbar:
        for idx, line in lines.iterrows():
            geometry = line.geometry
            if line.substation_1 in vsub_id_to_merged_id:
                merged_sub_id = vsub_id_to_merged_id[line.substation_1]
                lines.loc[idx, "substation_1"] = merged_sub_id  # type: ignore

                merged_sub_center = merged[merged.id == merged_sub_id].geometry.iloc[0].coords[0]  # type: ignore
                geometry = LineString([merged_sub_center, *geometry.coords[:]])
                lines.loc[idx, "geometry"] = geometry  # type: ignore

            if line.substation_2 in vsub_id_to_merged_id:
                merged_sub_id = vsub_id_to_merged_id[line.substation_2]
                lines.loc[idx, "substation_2"] = merged_sub_id  # type: ignore

                merged_sub_center = merged[merged.id == merged_sub_id].geometry.iloc[0].coords[0]  # type: ignore
                geometry = LineString([*geometry.coords[:], merged_sub_center])
                lines.loc[idx, "geometry"] = geometry  # type: ignore

            pbar.update(1)

    # Revert back to old geometry and crs
    merged.set_geometry("polygon", crs=crs, inplace=True)
    merged.drop(columns="point", inplace=True)

    real_subs = subs[subs["type"] != "virtual_substation"].copy()
    subs = gpd.GeoDataFrame(pd.concat([real_subs, merged], ignore_index=True), crs=crs, geometry="polygon")

    return lines, subs


def _merge_broken_lines(lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Find virtual buses where there are exactly two lines, and merge the lines into one new line.

    Parameters
    ----------
    - lines (GeoDataFrame): A representation of the line, with a geometry property of type LineString.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.

    Returns
    -------
    - lines (GeoDataFrame): The lines gdf with updated merged lines.
    - subs (GeoDataFrame): The substation gdf without the now redundant virtual buses.
    """
    virtual_subs = subs[subs["type"] == "virtual_substation"].copy()

    vsubs_to_remove = []
    for vsub in virtual_subs.itertuples():
        # Check if there are exactly two lines connected
        connected_lines = lines[(lines.substation_1 == vsub.id) | (lines.substation_2 == vsub.id)]
        if len(connected_lines) != 2:
            continue

        # Only merge if the number of circuits match on both lines.
        if len(connected_lines.circuits.unique()) != 1 and not connected_lines.circuits.isna().any():
            continue

        # Check that voltage level matches
        l1, l2 = connected_lines.iloc[0], connected_lines.iloc[1]
        if l1.voltage != l2.voltage:
            # This actually never happens (but I guess it could)
            continue

        other_sub = connected_lines.apply(
            lambda l, vsub_id=vsub.id: l["substation_1"] if l["substation_1"] != vsub_id else l["substation_2"], axis=1
        )
        if len(other_sub.unique()) == 1:  # type: ignore
            # Do not remove sub if both lines connect the same substations
            continue

        # Get new, merged line.
        merged_line = _merge_lines(connected_lines)
        lines.drop(index=connected_lines.index, inplace=True)

        lines = gpd.GeoDataFrame(pd.concat([lines, merged_line], ignore_index=True), crs=crs, geometry="geometry")

        vsubs_to_remove.append(vsub.Index)

    subs.drop(index=vsubs_to_remove, inplace=True)

    return lines, subs


def _merge_lines(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Merges the two given lines into one new line.

    Parameters
    ----------
    - lines (GeoDataFrame): A gdf with exactly two lines to be merged.

    Returns
    -------
    - merged_line (GeoDataFrame): A gdf with one row representing the merged line.

    Notes
    -----
    - Merges the ids by adding a hyphen between them.
    """
    assert len(lines) == 2

    l1, l2 = lines.iloc[0], lines.iloc[1]
    l1_coords, l2_coords = l1.geometry.coords, l2.geometry.coords

    merged_line = l1.combine_first(l2)

    # Merge geometry based on which substations match
    if l1.substation_2 == l2.substation_1:
        coords = l1_coords[:] + l2_coords[1:]
        s1, s2 = l1.substation_1, l2.substation_2
    elif l1.substation_1 == l2.substation_2:
        coords = l2_coords[:] + l1_coords[1:]
        s1, s2 = l2.substation_1, l1.substation_2
    elif l1.substation_1 == l2.substation_1:
        coords = l1_coords[::-1] + l2_coords[1:]
        s1, s2 = l1.substation_2, l2.substation_2
    elif l1.substation_2 == l2.substation_2:
        coords = l1_coords[:] + l2_coords[:-1][::-1]
        s1, s2 = l1.substation_1, l2.substation_1
    else:
        raise ValueError("No matching substations!")

    merged_line["geometry"] = LineString(coords)
    merged_line["substation_1"], merged_line["substation_2"] = s1, s2

    # Create merged id.
    merged_line["id"] = f"{l1.id}-{l2.id}"

    merged_line = gpd.GeoDataFrame([merged_line], geometry="geometry", crs=crs)

    return merged_line.astype({"circuits": "Int64"})


def _remove_final_step_virtual_buses(
    lines: gpd.GeoDataFrame, subs: gpd.GeoDataFrame
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Removes virtual buses that are used to split up lines right before they enter substations. Not strictly needed, but reduces complexity
    of the lines a lot.

    Parameters
    ----------
    - lines (GeoDataFrame): A representation of the line, with a geometry property of type LineString.
    - subs (GeoDataFrame): Each row is a substation with polygon geometry.

    Returns
    -------
    - lines (GeoDataFrame): The lines gdf without the final connecting lines.
    - subs (GeoDataFrame): The substation gdf without the virtual buses connecting the removed lines.

    Notes
    -----
    - Specifically, the lines that are removed are lines that are exactly one segment long, and that are connected
    to a virtual bus where there is one line coming in, and multiple lines going out that match the incoming line.
    This virtual bus is also removed. The incoming line is extended to the center of the substation.
    """
    virtual_subs = subs[subs["type"] == "virtual_substation"].copy()

    vsubs_to_remove = []
    with tqdm(total=len(virtual_subs), bar_format=tabbed_tqdm_format) as pbar:
        for vsub in virtual_subs.itertuples(index=False):
            pbar.update(1)
            # Get connected lines
            connected_lines = lines[(lines.substation_1 == vsub.id) | (lines.substation_2 == vsub.id)].copy()
            connected_lines["other_sub"] = connected_lines.apply(
                lambda l, vsub_id=vsub.id: l["substation_1"] if l["substation_1"] != vsub_id else l["substation_2"],
                axis=1,
            )
            counts = connected_lines.other_sub.value_counts()

            if len(counts) != 2 or min(counts) != 1:
                # Only remove virtual bus where there is only one line coming in from a different direction
                continue

            sub_to_merge_to = counts.index[0]
            lines_to_remove = connected_lines[connected_lines.other_sub == sub_to_merge_to]

            if not (lines_to_remove.geometry.apply(lambda l: len(l.coords)) == 2).all():
                # Only remove virtual bus if the lines to remove are exactly one segment long
                continue

            vsubs_to_remove.append(vsub.Index)

            lines.drop(index=lines_to_remove.index, inplace=True)

            # Update substation value on the remaining line
            line_to_move = connected_lines[connected_lines.other_sub != sub_to_merge_to].iloc[0]  # always exactly one
            sub_number = "substation_1" if line_to_move.substation_1 == vsub.id else "substation_2"
            lines.loc[line_to_move.name, sub_number] = sub_to_merge_to

            # Update geometry on the remaining line
            new_endpoint = subs[subs.id == sub_to_merge_to].iloc[0].point.coords[0]
            if sub_number == "substation_2":
                new_coords = [*line_to_move.geometry.coords[:], new_endpoint]
            else:
                new_coords = [new_endpoint, *line_to_move.geometry.coords[:]]
            lines.loc[line_to_move.name, "geometry"] = LineString(new_coords)  # type: ignore

    subs.drop(index=vsubs_to_remove, inplace=True)

    return lines, subs
