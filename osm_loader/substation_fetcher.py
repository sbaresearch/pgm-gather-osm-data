import json
import os

import requests

from util.utils import DataType, confirm_data_fetch


def get_raw_subs(date: str, type: DataType | None = None):
    overpass_url = "https://overpass-api.de/api/interpreter"

    features = {"substation_ways": 'way["power"="substation"]', "substation_relations": 'rel["power"="substation"]'}
    if type is not None:
        features = {key: value for key, value in features.items() if type in key}

    for name, filter in features.items():
        if not confirm_data_fetch(f"osm_data/{name}_{date}.json", name):
            continue

        query = f"""
            [out:json][date:"{date}"][timeout:500];
            area["ISO3166-1"="AT"]->.searchArea;
            (
            {filter}(area.searchArea);
            );
            out body geom;
        """

        headers = {"User-Agent": "pgm_get_powergrid_model"}

        response = requests.post(
            overpass_url,
            data={"data": query},
            headers=headers,
            timeout=(10, 600),
        )
        response.raise_for_status()  # Raise HTTPError for bad responses
        if "remark" in response.json():
            raise MemoryError("Propblem gathering data from OSM, probably ran out of memory. Try again later.")

        filepath = f"osm_data/{name}_{date}.json"
        parentfolder = os.path.dirname(filepath)
        if not os.path.exists(parentfolder):
            os.makedirs(parentfolder)

        with open(filepath, mode="w") as f:
            json.dump(response.json(), f, indent=2)
