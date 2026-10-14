from random import random

import geopandas as gpd
from shapely.affinity import translate


def add_visualization(
    line_gdf: gpd.GeoDataFrame, sub_gdf: gpd.GeoDataFrame, gens_gdf: gpd.GeoDataFrame
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    line_gdf = add_line_color(line_gdf)
    line_gdf = shift_lines(line_gdf)
    sub_gdf = add_sub_color(sub_gdf)
    gens_gdf = add_gen_color(gens_gdf)
    gens_gdf = change_gen_unit(gens_gdf)

    return line_gdf, sub_gdf, gens_gdf


def add_line_color(line_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    line_gdf = line_gdf.copy()

    line_gdf["stroke"] = "#00FF00"
    colors = {
        110000: "#00FF00",
        220000: "#FF0000",
        380000: "#0000FF",
    }

    line_gdf["stroke"] = line_gdf.voltage.apply(lambda v: colors[v])

    line_gdf["stroke-width"] = 2
    stroke_widths = {
        110000: 2,
        220000: 4,
        380000: 6,
    }

    line_gdf["stroke-width"] = line_gdf.voltage.apply(lambda v: stroke_widths[v])

    return line_gdf


def shift_lines(line_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    line_gdf = line_gdf.copy()
    max_shift = 0.00003
    line_gdf["geometry"] = line_gdf.geometry.apply(
        lambda x: translate(x, xoff=random() * max_shift, yoff=random() * max_shift)
    )

    return line_gdf


def add_sub_color(sub_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    sub_gdf = sub_gdf.copy()
    sub_gdf["fill"] = "#00FF00"
    sub_gdf.loc[sub_gdf["type"].str.contains("virtual"), ["fill"]] = "#999999"
    return sub_gdf


def add_gen_color(gens_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gens_gdf = gens_gdf.copy()
    gens_gdf.loc[gens_gdf["type"] == "way", ["fill"]] = "#FF0000"
    gens_gdf.loc[gens_gdf["type"] == "relation", ["fill"]] = "#FF9900"
    return gens_gdf


def change_gen_unit(gens_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gens_gdf = gens_gdf.copy()
    gens_gdf["output_MW"] = gens_gdf.output_w / 1e6
    gens_gdf.drop(columns="output_w", inplace=True)
    return gens_gdf
