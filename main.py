from datetime import datetime

from gather_data import gather_data
from util import config


def create_results_dir(name: str | None):
    from os import makedirs, path

    if not path.isdir("results"):
        makedirs("results")

    config.RESULTS_DIR = f"results/{name}"

    if path.exists(config.RESULTS_DIR):
        raise NameError("Stated path for result directory already exists")

    makedirs(config.RESULTS_DIR)

    if config.VISUALIZATION:  # only create dir if any maps/graphs will be created
        makedirs(config.RESULTS_DIR + "/visualisation")


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

    config.VERBOSE = args.verbose
    config.VISUALIZATION = args.visualization
    config.FETCH_NEW = args.fetch_new

    from os import makedirs, path

    if not path.isdir("osm_data"):
        makedirs("osm_data")

    # Translate date to correct format
    date = "" if args.date == "NAN" else args.date
    if date:
        d = datetime.fromisoformat(date)
        date = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"

    export_name = f"osm_data_{datetime.now()}"  # noqa: DTZ005
    if date:
        export_name = f"{export_name}_attic_{date}"
    create_results_dir(export_name)

    gather_data(date)
