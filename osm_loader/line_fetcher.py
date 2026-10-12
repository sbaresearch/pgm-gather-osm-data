import json
import os

import requests

from util.utils import confirm_data_fetch


def get_raw_lines(date: str):
    # TODO: Maybe also include relation["route"="power"]?
    if not confirm_data_fetch(f"osm_data/lines_{date}.json", "line"):
        return

    overpass_url = "https://overpass-api.de/api/interpreter"

    query = f"""
        [out:json][date:"{date}"][timeout:300];
        area["ISO3166-1"="AT"]->.searchArea;
        (
        way["power"~"line|cable"](area.searchArea);
        );
        out body geom;
    """

    headers = {"User-Agent": "nurzu_get_powergrid_model"}

    response = requests.post(overpass_url, data=query, headers=headers)
    response.raise_for_status()  # Raise HTTPError for bad responses
    if len(response.json()["elements"]) == 0:
        raise TimeoutError("Could not find any data; probably query timed out")

    filepath = f"osm_data/lines_{date}.json"
    parentfolder = os.path.dirname(filepath)
    if not os.path.exists(parentfolder):
        os.makedirs(parentfolder)

    with open(filepath, mode="w") as f:
        json.dump(response.json(), f, indent=2)
