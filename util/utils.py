import os

import geopandas as gpd
import numpy as np
from tqdm import tqdm

from util import config

crs = "EPSG:4326"  # lat lon (what OSM uses)
distance_crs = "EPSG:31287"  # Austria only
tabbed_tqdm_format = "        {l_bar}{bar}{r_bar}"


def export_gdf(gdf: gpd.GeoDataFrame, file_path: str):
    gdf.to_file(file_path)


def split_on_multiple(string: str, substrings: list[str]) -> list[str]:
    """
    Splits the given string on all substrings in the substrings list. Performs the same operation as
    str.split(substr) but for each substring in substrings.

    EXAMPLE: split_on_multiple("hi_my/name:is", ["_", "/", ":"]) -> ["hi", "my" "name", "is"]

    Parameters
    ----------
        - string (str): The string to split.
        - substrings: (list[str]): The substrings to split on.

    Returns
    -------
        list|None: Returns a list of voltage levels if any are found above the specified threshold.
        Returns None if "voltage" is not available in tags, or if no voltage level above the threshold is found.
    """

    return _recursive_split([string], substrings)


def _recursive_split(strings: list[str], substrings: list[str]) -> list[str]:
    if len(substrings) == 0:
        return strings

    res_strings = []
    for s in strings:
        res_strings.extend(s.split(substrings[0]))
    return _recursive_split(res_strings, substrings[1:])


def confirm_data_fetch(file_path: str, name: str):
    """Force the user to confirm that they want to gather data from OSM if it is already present"""
    if os.path.exists(file_path):
        answer = "n"
        while answer != "y".lower():
            answer = input(
                f"{name.title()} data seems to already be present. Are you sure you want to re-gather data from OSM? (y/n) "
            ).lower()
            if answer == "n":
                print(f"Not gathering new {name.lower()} data from OSM.")
                return False
    return True


def vprint(string: str):
    if config.VERBOSE:
        tqdm.write(string)


def _clamp_voltage(voltages: list[int] | None) -> list[int] | None:
    """
    Clamps voltage levels to the closest value in the clamp_values list (110000, 280000, 380000).

    Parameters
    ----------
    - voltages (list[int]): A list of voltage levels

    Returns
    -------
    closest (list[int]): A new list where each element from the input list is matched to the closest clamp value.
    """
    if voltages is None:
        return None

    clamp_values = [110000, 220000, 380000]
    differences = [[abs(vol - clamp) for clamp in clamp_values] for vol in voltages]
    closest = [clamp_values[np.argmin(diff)] for diff in differences]

    return closest
