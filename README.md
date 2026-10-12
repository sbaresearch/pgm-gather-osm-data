# Get OSM data
Gather data from OSM and create CSV files with the relevant data. Based on the method described in [*Modelling the high-voltage grid using open data for Europe and beyond*](https://www.nature.com/articles/s41597-025-04550-7) by B. Xiong at TU Berlin.

This (along with [Nurzu Load Flow Model](https://gitlab.sba-research.org/eris/nurzu-load-flow-model) and [Nurzu Load Flow Simulation](https://gitlab.sba-research.org/eris/nurzu-load-flow-simulation)) is part of a refactored and modularized successor to the now deprecated [nurzu-load-flow-analysis](https://gitlab.sba-research.org/johanna/nurzu-load-flow-analysis) repository.

## Overview
* Gathers substations, generators, and high and extra-high voltage power lines (110, 220, 380kV) from OSM.
* Can gather attic data (data as it was on OSM at a certain date).
* Processes these, by clarifying geometries, merging touching substations, and splitting lines into one object per system.
* Can create geojson visualizations of the gathered data.

## Results
The cleaned up final data is stored as 3 geopackages in the results folder, along with visualization data (if requested) in the appropriate folder.

## Installation

### 1. Install Python

Required version: **Python 3.13**  
Download from: [https://www.python.org/downloads/](https://www.python.org/downloads/)

### 2. Install dependencies

Navigate to the project folder and create a virtual environment. Then install the dependencies using
```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py [options]
```

### Available options

| Option                    | Description                                    |
| ------------------------- | ---------------------------------------------- |
| `-h`, `--help`            | Show help message and exit                     |
| `-v`, `--verbose`         | Print additional information                   |
| `-d`, `--date`            | Specify a date from which to gather attic data | 
| `-vis`, `--visualization` | Create GeoJSON files for visualization         |


### Example commands

* Gather most current OSM data and print additional information:

  ```bash
  python main.py -v
  ```

* Gather data as it was at the end of 2022 and create geojsons for visualization:

  ```bash
  python main.py -d 2023-01-01T00:00:00 -vis

## How it Works
See [*Modelling the high-voltage grid using open data for Europe and beyond*](https://www.nature.com/articles/s41597-025-04550-7) by B. Xiong for a detailed description of the main idea.

Gathers generator, substation, and power line data from OSM API. Cleans generators by fixing geometries and filtering out bad elements. Cleans substations in a similar way, but also merges substations that touch each other.

For the lines, they are cleaned up by fixing geometries and poor values. However, they are also split into different element based on the OSM specifications. For example, a line with the voltage value "380000, 110000" gets split into two lines, one with a voltage level of 380000 and one with 110000. They are then split when they go over substations, and subsequently connected to the substations at their ends. "Virtual buses" are created at the end of lines far away from any substations. Also includes handling of special cases in Vienna, of which there are many for older data.

## Related Projects

* **[Nurzu Load Flow Model](https://gitlab.sba-research.org/eris/nurzu-load-flow-model)**\
  Build a model of the Austrian power grid using this OSM data.

* **[Nurzu Load Flow Simulation](https://gitlab.sba-research.org/eris/nurzu-load-flow-simulation)**\
  Simulates power flow using the model produced by *Nurzu Load Flow Model*.

* **Deprecated:**\
  [Nurzu Load Flow Analysis](https://gitlab.sba-research.org/johanna/nurzu-load-flow-analysis) (*Nurzu Load Flow Model* and *Nurzu Load Flow Simulation* replace it)
