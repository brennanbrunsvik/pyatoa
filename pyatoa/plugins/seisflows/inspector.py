"""
A class to analyze the outputs of a Seisflows inversion by
looking at misfit information en masse and producing text files
and images related to analysis of data
"""
import os
import json
import pyasdf
import numpy as np
from glob import glob
from obspy.geodetics import gps2dist_azimuth

from pyatoa.utils.tools.calculate import abs_max
from pyatoa.utils.tools.srcrcv import eventid, lonlat_utm
from pyatoa.utils.asdf.extractions import count_misfit_windows
from pyatoa.plugins.seisflows.artist import Artist


class Inspector(Artist):
    """
    This plugin object will collect information from a Pyatoa run folder and
    allow the User to easily understand statistical information or generate
    statistical plots to help understand a seismic inversion
    
    Inherits plotting capabilities from the Artist class to reduce clutter.
    """
    def __init__(self, tag=None, path=None, misfits=True, srcrcv=True,
                 windows=True, utm=-60):
        """
        Inspector only requires the path to the datasets, it will then read in
        all the datasets and store the data internally. This is a long process
        but should only need to be done once.

        Allows parameters to determine what quantities are queried from dataset
        Inherits plotting functionality from the Visuals class

        :type misfits: bool
        :param misfits: collect misfit information
        :type srcrcv: bool
        :param srcrcv: collect coordinate information
        :type path_to_datasets: str
        :param path_to_datasets: path to the ASDFDataSets that were outputted
            by Pyaflowa in the Seisflows workflow
        """
        # If no tag given, create dictionaries based on datasets
        self.srcrcv = {}
        self.misfits = {}
        self.windows = {}
        self.utm = utm
        self._stations = None
        self._event_ids = None

        # If a tag is given, load rather than reading from datasets
        if tag is not None:
            self.load(tag)
        elif path is not None:
            dsfids = glob(os.path.join(path, "*.h5"))
            for i, dsfid in enumerate(dsfids):
                print(f"{dsfid}, {i}/{len(dsfids)}", end="...") 
                status = self.append(dsfid, windows, srcrcv, misfits)
                if status:
                    print("done")
                else:
                    print("error")

    @property
    def event_ids(self):
        """Return a list of all event ids"""
        if not self._event_ids:
            self.get_event_ids_stations()
        return self._event_ids

    @property
    def stations(self):
        """Return a list of all stations"""
        if not self._stations:
            self.get_event_ids_stations()
        return self._stations

    @property
    def models(self):
        """Return a list of all models"""
        return list(self.sort_misfits_by_model().keys())

    @property
    def mags(self):
        """Return a dictionary of event magnitudes"""
        return self.event_info("mag")

    @property
    def times(self):
        """Return a dictionary of event origin times"""
        return self.event_info("time")

    @property
    def depths(self):
        """Return a dictionary of event depths"""
        return self.event_info("depth_m")

    def get_event_ids_stations(self):
        """
        One-time retrieve lists of station names and event ids, based on the 
        fact that stations are separated by a '.'
        """
        event_ids, stations = [], []
        for key in self.srcrcv.keys():
            if "." in key:
                stations.append(key)
            else:
                event_ids.append(key)
        self._stations = stations
        self._event_ids = event_ids
    
    def append(self, dsfid, windows=True, srcrcv=True, misfits=True):
        """
        Append a new pyasdf.ASDFDataSet file to the current set of internal
        statistics

        :type dsfid: str
        :param dsfid: fid of the dataset
        :type windows: bool
        :param windows: get window info
        :type srcrcv: bool
        :param srcrcv: get srcrcv info
        :type misfits: bool
        :param misfits: get misfit info
        """
        try:
            with pyasdf.ASDFDataSet(dsfid) as ds:
                if windows:
                    self.get_windows(ds)
                if srcrcv:
                    self.get_srcrcv(ds)
                if misfits:
                    self.get_misfits(ds)
                return 1
        except OSError:
            print(f"{dsfid} already open")
            return 0

    def event_info(self, choice):
        """
        Return event information in a dictionary object

        :type choice: str
        :param choice: choice of key to query dictionary
        """
        info = {}
        for event in self.srcrcv.keys():
            info[event] = self.srcrcv[event][choice]
        return info

    def event_stats(self, model, choice="cc_shift_sec", sta_code=None,
                    event_id=None):
        """
        Return the number of measurements per event or station

        :param model:
        :param sta_code:
        :param event_id:
        :return:
        """
        events, msftval, nwins = [], [], []

        misfits = self.sort_windows_by_model()[model]
        for event in misfits.keys():
            if event_id and event != event_id:
                continue
            nwin = 0
            misfit = []
            for sta in misfits[event].keys():
                if sta_code and sta != sta_code:
                    continue
                for comp in misfits[event][sta].keys():
                    misfit += misfits[event][sta][comp][choice]
                    nwin += len(misfits[event][sta][comp][choice])

            events.append(event)
            msftval.append(misfit)
            nwins.append(nwin)

        # Sort and print
        zipped = list(zip(nwins, events, msftval))
        zipped.sort(reverse=False)
        nwins, events, msftval = zip(*zipped)

        for eid, nwin, msft in zip(events, nwins, msftval):
            print(f"{eid:>13}{nwin:>5d}{abs_max(msft):6.2f}")

        return events, nwins, msftval

    def window_values(self, model, choice):
        """
        Return a list of all time shift values for a given model

        :type model: str
        :param model: model to query e.g. 'm00'
        :type choice: str
        :param choice: key choice for window query
        :rtype list:
        :return: list of time shift values for a given model
        """
        choices = ["cc_shift_sec", "dlna", "max_cc", "length_s", "weight"]
        assert(choice in choices), f"choice must be in {choices}"

        ret = []
        windows = self.sort_windows_by_model()
        for event in windows[model]:
            for sta in windows[model][event]:
                for cha in windows[model][event][sta]:
                    ret += windows[model][event][sta][cha][choice]
        return ret

    def misfit_values(self, model):
        """
        Return a list of misfit values for a given model

        :type model: str
        :param model: model to query e.g. 'm00'
        :rtype list:
        :return: list of misfit values for a given model
        """
        misfit = []
        for event in self.misfits:
            for model_ in self.misfits[event]:
                if model_ != model:
                    continue
                for sta in self.misfits[event][model]:
                    misfit.append(self.misfits[event][model][sta]["msft"])
        return misfit

    def get_srcrcv(self, ds):
        """
        Get source receiver info including coordinates, distances and BAz
        from a given dataset.

        :type ds: pyasdf.ASDFDataSet
        :param ds: dataset to query for distances
        """
        # Initialize the event as a dictionary
        eid = eventid(ds.events[0])

        # Get UTM projection of event coordinates
        ev_x, ev_y = lonlat_utm(
            lon_or_x=ds.events[0].preferred_origin().longitude,
            lat_or_y=ds.events[0].preferred_origin().latitude,
            utm_zone=self.utm, inverse=False
        )

        self.srcrcv[eid] = {"lat": ds.events[0].preferred_origin().latitude,
                            "lon": ds.events[0].preferred_origin().longitude,
                            "depth_m": ds.events[0].preferred_origin().depth,
                            "time": str(ds.events[0].preferred_origin().time),
                            "mag": ds.events[0].preferred_magnitude().mag,
                            "utm_x": ev_x,
                            "utm_y": ev_y
                            }

        # Loop through all the stations in the dataset
        for sta, sta_info in ds.get_all_coordinates().items():
            # Append station location information one-time to dictionary
            if sta not in self.srcrcv:
                sta_x, sta_y = lonlat_utm(lon_or_x=sta_info["longitude"],
                                          lat_or_y=sta_info["latitude"],
                                          utm_zone=self.utm, inverse=False
                                          )
                self.srcrcv[sta] = {"lat": sta_info["latitude"],
                                    "lon": sta_info["longitude"],
                                    "elv_m": sta_info["elevation_in_m"],
                                    "utm_x": sta_x,
                                    "utm_y": sta_y
                                    }

            # Append src-rcv distance and backazimuth to specific event
            gcd, _, baz = gps2dist_azimuth(lat1=self.srcrcv[eid]["lat"],
                                           lon1=self.srcrcv[eid]["lon"],
                                           lat2=self.srcrcv[sta]["lat"],
                                           lon2=self.srcrcv[sta]["lon"]
                                           )
            self.srcrcv[eid][sta] = {"dist_km": gcd * 1E-3, "baz": baz}

    def get_misfits(self, ds):
        """
        Get Misfit information from a dataset

        :type ds: pyasdf.ASDFDataSet
        :param ds: dataset to query for misfit
        """
        eid = eventid(ds.events[0])

        self.misfits[eid] = {}
        for model in ds.auxiliary_data.AdjointSources.list():
            self.misfits[eid][model] = {}
            num_win = count_misfit_windows(ds, model, count_by_stations=True)

            # For each station, determine the number of windows and total misfit
            for station in ds.auxiliary_data.AdjointSources[model]:
                sta_id = station.parameters["station_id"]
                misfit = station.parameters["misfit_value"]

                # One time initiatation of a new dictionary object
                if sta_id not in self.misfits[eid][model]:
                    self.misfits[eid][model][sta_id] = {"msft": 0,
                                                        "nwin": num_win[sta_id]
                                                        }

                # Append the total number of windows, and the total misfit
                self.misfits[eid][model][sta_id]["msft"] += misfit

            # Scale the misfit of each station by the number of windows
            for sta_id in self.misfits[eid][model].keys():
                self.misfits[eid][model][sta_id]["msft"] /= \
                                    2 * self.misfits[eid][model][sta_id]["nwin"]
                
    def get_windows(self, ds):
        """
        Get Window information from auxiliary_data.MisfitWindows
        
        :return: 
        """
        eid = eventid(ds.events[0])
    
        self.windows[eid] = {}
        for model in ds.auxiliary_data.MisfitWindows.list():
            self.windows[eid][model] = {}

            # For each station, determine the number of windows and total misfit
            for window in ds.auxiliary_data.MisfitWindows[model]:
                cha_id = window.parameters["channel_id"]
                net, sta, loc, cha = cha_id.split(".")
                sta_id = f"{net}.{sta}"

                dlna = window.parameters["dlnA"]
                weight = window.parameters["window_weight"]
                max_cc = window.parameters["max_cc_value"]
                length_s = (window.parameters["relative_endtime"] -
                            window.parameters["relative_starttime"]
                            )
                rel_start = window.parameters["relative_starttime"]
                rel_end = window.parameters["relative_endtime"]
                cc_shift_sec = window.parameters["cc_shift_in_seconds"]

                # One time initiatations of a new dictionary object
                win = self.windows[eid][model]
                if sta_id not in win:
                    win[sta_id] = {}
                if cha not in self.windows[eid][model][sta_id]:
                    win[sta_id][cha] = {"cc_shift_sec": [], "dlna": [],
                                        "weight": [], "max_cc": [],
                                        "length_s": [], "rel_start": [],
                                        "rel_end": []
                                        }

                # Append values from the parameters into dictionary object
                win[sta_id][cha]["dlna"].append(dlna)
                win[sta_id][cha]["weight"].append(weight)
                win[sta_id][cha]["max_cc"].append(max_cc)
                win[sta_id][cha]["length_s"].append(length_s)
                win[sta_id][cha]["rel_end"].append(rel_end)
                win[sta_id][cha]["rel_start"].append(rel_start)
                win[sta_id][cha]["cc_shift_sec"].append(cc_shift_sec)

    def save(self, tag):
        """
        Save the downloaded attributes into JSON files for re-loading

        :type tag: str
        :param tag: unique naming tag for saving json files
        """
        def write(self, suffix):  # NOQA
            """
            Convenience function to save internal attributes
            """
            obj = getattr(self, suffix)
            if obj:
                with open(f"{tag}_{suffix}.json", "w") as f:
                    print(f"writing {suffix}")
                    json.dump(obj, f, indent=4, sort_keys=True)

        for s in ["srcrcv", "misfits", "windows"]:
            write(self, s)

    def load(self, tag):
        """
        Load previously saved attributes to avoid re-processing data

        :type tag: str
        :param tag: tag to look for json files
        """
        def read(self, suffix):  # NOQA
            """
            Convenience function to read in saved files

            :type suffix: str
            :param suffix: suffix of file name
            """
            print(f"reading {suffix} file", end="... ")
            try:
                with open(f"{tag}_{suffix}.json", "r") as f:
                    setattr(self, suffix, json.load(f))
                    print("found")
            except FileNotFoundError:
                print("not found")
                pass

        for s in ["srcrcv", "misfits", "windows"]:
            read(self, s)

    def sort_by_window(self, model, choice="cc_shift_sec"):
        """
        Sort the Inspector by the largest time shift
        """
        values, info = [], []

        windows = self.sort_windows_by_model()[model]
        for event in windows:
            for sta in windows[event]:
                for comp in windows[event][sta]:
                    for value in windows[event][sta][comp][choice]:
                        values.append(value)
                        info.append((event, sta, comp))
        # sort by value
        values, info = (list(_) for _ in zip(*sorted(zip(values, info))))

        return values, info

    def sort_misfits_by_station(self):
        """
        Sort the misfits collected by get_misfits() by model rather than
        by event. Returns a dictionary of misfit sorted by model

        :rtype dict:
        :return: misfits sorted by model
        """
        misfits = {}
        for event in self.misfits:
            for model in self.misfits[event]:
                if model not in misfits:
                    misfits[model] = {}
                for sta in self.misfits[event][model]:
                    if sta not in misfits[model]:
                        misfits[model][sta] = {"msft": 0, "nwin": 0,
                                               "nevents": 0}

                    # Append misfit info from each station-event in same model
                    misfits[model][sta]["msft"] += (
                        self.misfits[event][model][sta]["msft"]
                    )
                    misfits[model][sta]["nwin"] += (
                        self.misfits[event][model][sta]["nwin"]
                    )
                    misfits[model][sta]["nevents"] += 1

            # Scale the total misfit per station by the number of events
            for sta in misfits[model]:
                misfits[model][sta]["msft"] /= misfits[model][sta]["nevents"]

        return misfits

    def sort_misfits_by_model(self):
        """
        Rearrage misfits by model rather than event

        :rtype dict:
        :return: misfits sorted by model
        """
        misfits = {}
        for event in self.misfits:
            for model in self.misfits[event]:
                if model not in misfits:
                    misfits[model] = {}
                if event not in misfits[model]:
                    misfits[model][event] = {}
                misfits[model][event] = self.misfits[event][model]

        return misfits

    def sort_windows_by_model(self):
        """
        Rearrage windows by model rather than event

        :rtype dict:
        :return: windows sorted by model
        """
        windows = {}
        for event in self.windows:
            for model in self.windows[event]:
                if model not in windows:
                    windows[model] = {}
                if event not in windows[model]:
                    windows[model][event] = {}
                windows[model][event] = self.windows[event][model]

        return windows


