#!/usr/bin/env python3
"""
Main workflow components of Pyatoa.
Manager is the central workflow control object. It calls on mid and low level
classes to gather data, and then runs these through Pyflex for misfit window
identification, and then into Pyadjoint for misfit quantification. Config class
required to set the necessary parameters

Crate class is a simple data storage object which is easily emptied and filled
such that the manager can remain relatively high level and not get bogged down
by excess storage requirements. Crate also feeds flags to the manager to
signal which processes have already occurred in the workflow.

TODO: create a moment tensor object that can be attached to the event object
"""
import warnings

import obspy
import pyflex
import pyadjoint
import numpy as np
from obspy.signal.filter import envelope

from pyatoa import logger
from pyatoa.utils.asdf.additions import write_adj_src_to_asdf
from pyatoa.utils.gathering.data_gatherer import Gatherer
from pyatoa.utils.operations.source_receiver import gcd_and_baz
from pyatoa.utils.operations.formatting import create_window_dictionary, \
     channel_codes
from pyatoa.utils.processing.preprocess import preproc, trimstreams
from pyatoa.utils.processing.synpreprocess import stf_convolve_gaussian
from pyatoa.utils.configurations.external import set_pyflex_station_event


class Crate:
    """
    An internal storage class for clutter-free rentention of data for individual
    stations. Simple flagging system for quick glances at workflow progress.

    Mid-level object that is called and manipulated by the Manager class.
    """
    def __init__(self, station_code=None):
        """
        :type station_code: str
        :param station_code: Station code following SEED naming convention.
            This must be in the form NN.SSSS.LL.CCC (N=network, S=station,
            L=location, C=channel). Allows for wildcard naming. By default
            the pyatoa workflow wants three orthogonal components in the N/E/Z
            coordinate system. Example station code: NZ.OPRZ.10.HH?
        :type st_obs: obspy.core.stream.Stream
        :param st_obs: Stream object containing waveforms of observations
        :type st_syn: obspy.core.stream.Stream
        :param st_syn: Stream object containing waveforms of observations
        :type inv: obspy.core.inventory.Inventory
        :param inv: Inventory that should only contain the station of interest,
            it's relevant channels, and response information
        :type event: obspy.core.event.Event
        :param event: An event object containing relevant earthquake information
        :type windows: dict of pyflex.Window objects
        :param windows: misfit windows calculated by Pyflex, stored in a
            dictionary based on component naming
        :type adj_srcs: dict of pyadjoint.AdjointSource objects
        :param adj_srcs: adjoint source waveforms stored in dictionaries

            (i.e. first column in the .sem? file) are shifted compared to the
            CMTSOLUTION, in units of seconds.
        :type *flag: bool
        :param *_flag: if * is present in the Crate, or if * has been processed
        """
        # Objects containing relevant information
        self.station_code = station_code
        self.st_obs = None
        self.st_syn = None
        self.inv = None
        self.event = None
        self.windows = None
        self.staltas = None
        self.adj_srcs = None
        
        # Internally used statistics 
        self.number_windows = None
        self.total_misfit = None        

        # Flags to show status of workflow
        self.event_flag = False
        self.st_obs_flag = False
        self.st_syn_flag = False
        self.inv_flag = False
        self.obs_process_flag = False
        self.syn_process_flag = False
        self.syn_shift_flag = False
        self.pyflex_flag = False
        self.pyadjoint_flag = False

    def check_flags(self):
        """
        Update flags based on what is available in the crate. The 3 in the
        stream process flags comes from the 2 steps taken before preprocessing,
        downsampling and trimming
        """
        # make sure observed waveforms are Stream objects
        # set flags for easy determination of status
        if isinstance(self.st_obs, obspy.Stream):
            self.st_obs_flag = len(self.st_obs)
            self.obs_process_flag = (
                    hasattr(self.st_obs[0].stats, "processing") and
                    len(self.st_obs[0].stats.processing) >= 3
            )
        else:
            self.st_obs_flag, self.obs_process_flag = False, False

        # make sure sytnhetic waveforms are Stream objects
        if isinstance(self.st_syn, obspy.Stream):
            self.st_syn_flag = len(self.st_syn)
            self.syn_process_flag = (
                    hasattr(self.st_syn[0].stats, "processing") and
                    len(self.st_syn[0].stats.processing) >= 3
            )
        else:
            self.st_syn_flag, self.syn_process_flag = False, False

        # check to see if inv is an Inventory
        if isinstance(self.inv, obspy.Inventory):
            self.inv_flag = "{net}.{sta}".format(net=self.inv[0].code,
                                                 sta=self.inv[0][0].code)
        else:
            self.inv_flag = False

        # check to see if event is an Event object
        if isinstance(self.event, obspy.core.event.Event):
            self.event_flag = self.event.resource_id
        else:
            self.event_flag = False

        # If pyflex and pyadjoint are run, the crate will have these
        self.pyflex_flag = isinstance(self.windows, dict)
        self.pyadjoint_flag = isinstance(self.adj_srcs, dict)

    def check_full(self):
        """
        A quick check to make sure the crate is full
        :return: bool
        """
        checkfull = bool(
            self.st_obs and self.st_syn and self.inv and self.event)
        return checkfull


class Manager:
    """
    Core object within Pyatoa.

    Workflow management function that internally calls on all other objects
    within the package in order to gather, process and analyze waveform data.
    """
    def __init__(self, config, ds=None, empty=False):
        """
        If no pyasdf dataset is given in the initiation of the Manager, all
        data fetching will happen via given pathways in the config file,
        or through external getting via FDSN pathways

        :type config: pyatoa.core.config.Config
        :param config: configuration object that contains necessary parameters
            to run through the Pyatoa workflow
        :type ds: pyasdf.asdf_data_set.ASDFDataSet
        :param ds: ASDF data set from which to read and write data
        :type gatherer: pyatoa.utils.gathering.data_gatherer.Gatherer
        :param gatherer: gathering function used to get and fetch data
        :type crate: pyatoa.core.Manager.Crate
        :param crate: Crate to hold all your information
        """
        self.config = config
        self.ds = ds
        self.gatherer = None
        self.crate = Crate()
        if not empty:
            self.launch()

    def __str__(self):
        """
        Print statement shows available information inside the workflow.
        """
        self.crate.check_flags()
        return ("CRATE\n"
                "\tEvent:                     {event}\n"
                "\tInventory:                 {inventory}\n"
                "\tObserved Stream(s):        {obsstream}\n"
                "\tSynthetic Stream(s):       {synstream}\n"
                "MANAGER\n"
                "\tObs Data Preprocessed:       {obsproc}\n"
                "\tSyn Data Preprocessed:       {synproc}\n"
                "\tSynthetic Data Shifted:      {synshift}\n"
                "\tPyflex runned:               {pyflex}\n"
                "\tPyadjoint runned:            {pyadjoint}\n"
                ).format(event=self.crate.event_flag,
                         obsstream=self.crate.st_obs_flag,
                         synstream=self.crate.st_syn_flag,
                         inventory=self.crate.inv_flag,
                         obsproc=self.crate.obs_process_flag,
                         synproc=self.crate.syn_process_flag,
                         synshift=self.crate.syn_shift_flag,
                         pyflex=self.crate.pyflex_flag,
                         pyadjoint=self.crate.pyadjoint_flag
                         )

    def reset(self, choice="hard"):
        """
        To avoid user interaction with the Crate class.
        Convenience function to instantiate a new Crate, and hence start the
        workflow from the start without losing your event or gatherer.
        :type choice: str
        :param choice: hard or soft reset, soft reset does not re-instantiate 
            gatherer class, and leaves the same event. Useful for short workflow
        """
        self.crate = Crate()
        if choice == "soft":
            self.launch(reset=True)

    @property
    def event(self):
        return self.gatherer.event

    @property
    def st(self):
        if isinstance(self.crate.st_syn, obspy.Stream) and \
                isinstance(self.crate.st_obs, obspy.Stream):
            return self.crate.st_syn + self.crate.st_obs
        elif isinstance(self.crate.st_syn, obspy.Stream) and \
                not isinstance(self.crate.st_obs, obspy.Stream):
            return self.crate.st_syn
        elif isinstance(self.crate.st_obs, obspy.Stream) and \
                not isinstance(self.crate.st_syn, obspy.Stream):
            return self.crate.st_obs
        else:
            return None

    @property
    def st_obs(self):
        return self.crate.st_obs

    @property
    def st_syn(self):
        return self.crate.st_syn

    @property
    def inv(self):
        return self.crate.inv

    @property
    def windows(self):
        return self.crate.windows

    @property
    def adj_srcs(self):
        return self.crate.adj_srcs

    def check_full(self):
        """
        check full from crate
        :return:
        """
        return self.crate.check_full()

    def overwrite(self, choice):
        """
        To manually overwrite the managers' windows, avoids user interaction
        with the Crate class. Check if windows are actually pyflex Windows
        :type windows: dict
        :param windows: dict of windows to overwrite
        """
        if choice == "windows":
            self.crate.windows = windows

    def launch(self, reset=False, set_event=None):
        """
        Initiate the prerequisite parts of the Manager class. Populate with
        an obspy event object which is gathered from FDSN by default.
        Allow user to provide their own ObsPy event object so that the
        Gatherer does not need to query FDSN
        :type reset: bool
        :param reset: Reset the Gatherer class for a new run
        :type set_event: obspy.core.event.Event
        :param set_event: if given, will bypass gathering the event and manually
            set to the user given event
        """
        # Launch the gatherer
        if (self.gatherer is None) or reset:
            logger.info("initiating/resetting gatherer")
            self.gatherer = Gatherer(config=self.config, ds=self.ds)

        # Populate with an event
        if set_event:
            self.gatherer.event = set_event
            self.crate.event = self.gatherer.event
        # If no event given by user, query FDSN
        else:
            if self.gatherer.event is not None:
                self.crate.event = self.gatherer.event
            else:
                if self.config.event_id is not None:
                    self.crate.event = self.gatherer.gather_event()

    def gather_data(self, station_code, overwrite=False):
        """
        Launch a gatherer object and gather event, station and waveform
        information given a station code. Fills the crate based on information
        most likely to be available (we expect an event to be available more
        often than waveform data).
        Catches general exceptions along the way, stops gathering if errors.

        :type station_code: str
        :param station_code: Station code following SEED naming convention.
            This must be in the form NN.SSSS.LL.CCC (N=network, S=station,
            L=location, C=channel). Allows for wildcard naming. By default
            the pyatoa workflow wants three orthogonal components in the N/E/Z
            coordinate system. Example station code: NZ.OPRZ.10.HH?
        :type overwrite: bool
        :param overwrite: user choice to overwrite existing objects in a crate.
            Useful e.g. when you only gather obs and failed on gathering syn,
            change some parameters and want to gather again without wasting
            time to gather stuff you already have.
        """
        # if overwriting, don't check the crate before gathering new data
        if overwrite:
            logger.info("Overwrite protection: OFF")
            try:
                self.crate.station_code = station_code
                logger.info("GATHERING {station} for {event}".format(
                    station=station_code, event=self.config.event_id)
                )
                logger.info("gathering station information")
                self.crate.inv = self.gatherer.gather_station(station_code)
                logger.info("gathering observation waveforms")
                self.crate.st_obs = self.gatherer.gather_observed(station_code)
                logger.info("gathering synthetic waveforms")
                self.crate.st_syn = self.gatherer.gather_synthetic(station_code)

            except Exception as e:
                print(e)
                return
        # if not overwriting, don't gather new data if crate already has data
        else:
            logger.info("Overwrite protection: ON")
            try:
                if not self.crate.station_code:
                    self.crate.station_code = station_code
                    logger.info("GATHERING {station} for {event}".format(
                        station=station_code, event=self.config.event_id)
                    )
                else:
                    logger.info("Crate already contains: {station}".format(
                        station=self.crate.station_code)
                    )
                if not self.crate.inv:
                    logger.info("gathering station information")
                    self.crate.inv = self.gatherer.gather_station(station_code)
                else:
                    logger.info("Crate already contains: {net}.{sta}".format(
                        net=self.crate.inv[0].code,
                        sta=self.crate.inv[0][0].code)
                    )
                if not self.crate.st_obs:
                    logger.info("gathering observation waveforms")
                    self.crate.st_obs = self.gatherer.gather_observed(
                        station_code)
                else:
                    logger.info(
                        "Crate already contains: {obs} obs. waveforms".format(
                            obs=len(self.crate.st_obs))
                    )
                if not self.crate.st_syn:
                    logger.info("gathering synthetic waveforms")
                    self.crate.st_syn = self.gatherer.gather_synthetic(
                        station_code)
                else:
                    logger.info(
                        "Crate already contains: {syn} syn. waveforms".format(
                            syn=len(self.crate.st_syn))
                    )
            except Exception as e:
                print(e)
                return

    def preprocess(self):
        """
        Preprocess observed and synthetic data in place on waveforms in crate.
        """
        # Pre-check to see if data has already been gathered
        if not (isinstance(self.crate.st_obs, obspy.Stream) and
                isinstance(self.crate.st_syn, obspy.Stream)
                ):
            warnings.warn("cannot preprocess, no waveform data", UserWarning)
            return

        # Process observation waveforms
        logger.info("preprocessing observation data")
        # If set in configs, rotate based on src rcv lat/lon values
        if self.config.rotate_to_rtz:
            _, baz = gcd_and_baz(self.crate.event, self.crate.inv)
        else:
            baz = None

        # Adjoint sources require the same sampling_rate as the synthetics
        sampling_rate = self.crate.st_syn[0].stats.sampling_rate

        # Run external preprocessing script
        self.crate.st_obs = preproc(self.crate.st_obs, inv=self.crate.inv,
                                    resample=sampling_rate,
                                    pad_length_in_seconds=20, back_azimuth=baz,
                                    output=self.config.unit_output,
                                    filter_bounds=[self.config.min_period,
                                                   self.config.max_period],
                                    corners=4
                                    )

        # Mid-check to see if preprocessing failed
        if not isinstance(self.crate.st_obs, obspy.Stream):
            warnings.warn("obs data could not be processed", UserWarning)
            return
        
        # Run external synthetic waveform preprocesser
        logger.info("preprocessing synthetic data")
        self.crate.st_syn = preproc(self.crate.st_syn, resample=None,
                                    pad_length_in_seconds=20,
                                    output=self.config.unit_output,
                                    back_azimuth=baz, corners=4,
                                    filter_bounds=[self.config.min_period,
                                                   self.config.max_period]
                                    )
        
        # Mid-check to see if preprocessing failed
        if not isinstance(self.crate.st_syn, obspy.Stream):
            warnings.warn("syn data could not be processed", UserWarning)
            return

        # Trim observations and synthetics to the length of synthetics
        self.crate.st_obs, self.crate.st_syn = trimstreams(
            st_a=self.crate.st_obs, st_b=self.crate.st_syn, force="b")

        # Retrieve the first timestamp in the .sem? file from Specfem
        self.crate.time_offset = (self.crate.st_syn[0].stats.starttime -
                                  self.crate.event.preferred_origin().time
                                  )

        # Convolve synthetic data with a gaussian source-time-function
        try:
            half_duration = (self.crate.event.focal_mechanisms[0].
                             moment_tensor.source_time_function.duration) / 2

            self.crate.st_syn = stf_convolve_gaussian(
                st=self.crate.st_syn, half_duration=half_duration,
                time_shift=False
            )
            self.crate.syn_shift_flag = True  # TODO: make this flag smarter
        except AttributeError:
            print("half duration value not found in event")

    def run_pyflex(self):
        """
        Call Pyflex to calculate best fitting misfit windows given observation
        and synthetic data in the crate. Return dictionaries of window objects,
        as well as STA/LTA traces, to the crate. If a pyasdf dataset is present,
        save misfit windows in as auxiliary data.
        If no misfit windows are found for a given station, throw a warning
        because pyadjoint won't run.
        Pyflex configuration is given by the config as a list of values with
        the following descriptions:

        i  Standard Tuning Parameters:
        0: water level for STA/LTA (short term average/long term average)
        1: time lag acceptance level
        2: amplitude ratio acceptance level (dlna)
        3: normalized cross correlation acceptance level
        i  Fine Tuning Parameters
        4: c_0 = for rejection of internal minima
        5: c_1 = for rejection of short windows
        6: c_2 = for rejection of un-prominent windows
        7: c_3a = for rejection of multiple distinct arrivals
        8: c_3b = for rejection of multiple distinct arrivals
        9: c_4a = for curtailing windows w/ emergent starts and/or codas
        10:c_4b = for curtailing windows w/ emergent starts and/or codas
        """
        # Pre-check to see if data has already been gathered
        if not (isinstance(self.crate.st_obs, obspy.Stream) and
                isinstance(self.crate.st_syn, obspy.Stream)
                ):
            warnings.warn("cannot run Pyflex, no waveform data")
            return

        # Create Pyflex Station and Event objects
        pf_station, pf_event = set_pyflex_station_event(
            inv=self.crate.inv, event=self.crate.event
        )

        # Empties to see if no windows were collected, windows and staltas
        # saved as dictionary objects by component name
        empties, number_windows = 0, 0
        windows, staltas = {}, {}
        for comp in self.config.component_list:
            try:
                # Run Pyflex to select misfit windows as Window objects
                window = pyflex.select_windows(
                    observed=self.crate.st_obs.select(component=comp),
                    synthetic=self.crate.st_syn.select(component=comp),
                    config=self.config.pyflex_config[1], event=pf_event,
                    station=pf_station, windows_filename="./{}".format(comp)
                    )
            except IndexError:
                window = []

            # Run Pyflex to collect STA/LTA information for plotting
            stalta = pyflex.stalta.sta_lta(
                data=envelope(self.crate.st_syn.select(component=comp)[0].data),
                dt=self.crate.st_syn.select(component=comp)[0].stats.delta,
                min_period=self.config.min_period
                )
            staltas[comp] = stalta
            number_windows += len(window)
            logger.info("{0} window(s) for comp {1}".format(len(window), comp))

            # If pyflex returns null, move on
            if not window:
                empties += 1
                continue
            else:
                windows[comp] = window

        # Let the User know that no windows were found for this station
        if empties == len(self.config.component_list):
            warnings.warn("Empty windows", UserWarning)

        # Store information in crate for Pyadjoint and plotting
        self.crate.windows = windows
        self.crate.staltas = staltas
        self.crate.number_windows = number_windows

        # Let the user know the outcomes of Pyflex
        logger.info("NUMBER WINDOWS {}".format(number_windows))
        print("{} window(s) total found".format(number_windows))
    
        # If an ASDFDataSet is given, save the windows into auxiliary_data
        if self.ds is not None and number_windows != 0:
            logger.info("Saving misfit windows to PyASDF")
            for comp in windows.keys():
                for i, window in enumerate(windows[comp]):
                    tag = "{mod}/{net}_{sta}_{cmp}_{num}".format(
                        net=self.crate.st_obs[0].stats.network,
                        sta=self.crate.st_obs[0].stats.station,
                        cmp=comp, mod=self.config.model_number, num=i)
                    wind_dict = create_window_dictionary(window)
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        # Auxiliary needs data, give it a bool; auxis love bools
                        self.ds.add_auxiliary_data(data=np.array([True]),
                                                   data_type="MisfitWindows",
                                                   path=tag,
                                                   parameters=wind_dict
                                                   )

    def run_pyadjoint(self):
        """
        Run pyadjoint on observation and synthetic data given misfit windows
        calculated by pyflex. Method for caluculating misfit set in config,
        pyadjoint config set in external configurations. Returns a dictionary
        of adjoint sources based on component. Saves resultant dictionary into
        the crate, as well as to a pyasdf dataset if given.

        NOTE: This is not in the PyAdjoint docs, but in
        pyadjoint.calculate_adjoint_source, the window needs to be a list of
        lists, with each list containing the [left_window,right_window];
        each window argument should be given in units of time (seconds)

        NOTE2: This version of Pyadjoint is located here:

        https://github.com/computational-seismology/pyadjoint/tree/dev

        Lion's version of Pyadjoint does not contain some of these functions
        """
        # Precheck to see if Pyflex has been run already
        if (self.crate.windows is None) or (isinstance(self.crate.windows, dict)
                                            and not len(self.crate.windows)
                                            ):
            warnings.warn("can't run Pyadjoint, no Pyflex outputs", UserWarning)
            return

        logger.info("running Pyadjoint for type {} ".format(
            self.config.pyadjoint_config[0])
        )

        # Iterate over given windows produced by Pyflex
        total_misfit = 0
        adjoint_sources = {}
        for key in self.crate.windows:
            adjoint_windows = []

            # Prepare window indices to give to Pyadjoint
            for win in self.crate.windows[key]:
                adj_win = [win.left * self.crate.st_obs[0].stats.delta,
                           win.right * self.crate.st_obs[0].stats.delta]
                adjoint_windows.append(adj_win)

            # Run Pyadjoint to retrieve adjoint source Objects
            adj_src = pyadjoint.calculate_adjoint_source(
                adj_src_type=self.config.pyadjoint_config[0],
                observed=self.crate.st_obs.select(component=key)[0],
                synthetic=self.crate.st_syn.select(component=key)[0],
                config=self.config.pyadjoint_config[1], window=adjoint_windows,
                plot=False
                )
            
            # Save adjoint sources in dictionary
            adjoint_sources[key] = adj_src
            logger.info("{misfit:.3f} misfit for component {comp} found".format(
                        misfit=adj_src.misfit, comp=key)
                        )
            total_misfit += adj_src.misfit

            # If ASDFDataSet given, save adjoint source into auxiliary data
            if self.ds is not None:
                logger.info("saving adjoint sources {} to PyASDF".format(key))
                with warnings.catch_warnings():
                    tag = "{mod}/{net}_{sta}_{ban}X{cmp}".format(
                        mod=self.config.model_number, net=adj_src.network,
                        sta=adj_src.station,
                        ban=channel_codes(self.crate.st_syn[0].stats.delta),
                        cmp=adj_src.component[-1]
                        )
                    warnings.simplefilter("ignore")
                    write_adj_src_to_asdf(adj_src, self.ds, tag,
                                          time_offset=self.crate.time_offset)
        
        # Save adjoint source into crate for plotting
        self.crate.adj_srcs = adjoint_sources
        
        # Save total misfit for this station in the crate
        # Misfit calucalated a la Tape (2010) Eq. 6
        self.crate.total_misfit = 0.5 * total_misfit/self.crate.number_windows

        # Let the user know the outcome of Pyadjoint
        logger.info("TOTAL MISFIT {:.3f}".format(self.crate.total_misfit)) 
        print("{} total misfit".format(self.crate.total_misfit))

    def plot_wav(self, **kwargs):
        """
        Waveform plots for all given components of the crate.
        If specific components are not given (e.g. adjoint source waveform),
        they are omitted from the final plot. Plotting should be dynamic, i.e.
        if only 2 components are present in the streams, only two subplots
        should be generated in the figure.
        :type show: bool
        :param show: show the plot once generated, defaults to False
        :type save: str
        :param save: absolute filepath and filename if figure should be saved
        :type figsize: tuple of floats
        :param figsize: length and width of the figure
        :type dpi: int
        :param dpi: dots per inch of the figure
        """
        # Precheck for waveform data
        if not (isinstance(self.crate.st_obs, obspy.Stream) and
                isinstance(self.crate.st_syn, obspy.Stream)
                ):
            warnings.warn("cannot plot waveforms, no waveform data",
                          UserWarning)
            return

        # Plotting functions contained in submodule
        from pyatoa.visuals.waveforms import window_maker
        show = kwargs.get("show", True)
        save = kwargs.get("save", None)
        figsize = kwargs.get("figsize", (11.69, 8.27))
        dpi = kwargs.get("dpi", 100)
        append_title = kwargs.get("append_title", "")

        # Calculate the seismogram length
        from pyatoa.utils.operations.source_receiver import seismogram_length
        length_s = seismogram_length(
            distance_km=gcd_and_baz(self.crate.event, self.crate.inv[0][0])[0],
            slow_wavespeed_km_s=2, binsize=50, minimum_length=100
        )
        
        # Call on window making function to produce waveform plots
        window_maker(
            st_obs=self.crate.st_obs, st_syn=self.crate.st_syn,
            windows=self.crate.windows, staltas=self.crate.staltas,
            adj_srcs=self.crate.adj_srcs, length_s=length_s,
            time_offset=self.crate.time_offset,
            stalta_wl=self.config.pyflex_config[1].stalta_waterlevel,
            unit_output=self.config.unit_output,
            config=self.config, figsize=figsize, 
            total_misfit=self.crate.total_misfit,
            append_title=append_title,
            dpi=dpi, show=show, save=save
        )

    def plot_map(self, **kwargs):
        """
        Map plot showing a map of the given target region. All stations that
        show data availability (according to the station master list) are
        plotted as open markers. Event is plotted as a beachball if a moment
        tensor is given, station of interest highlighted, both are connected
        with a dashed line.
        Source receier information plotted in lower right hand corner of map.
        :type show: bool
        :param show: show the plot once generated, defaults to False
        :type save: str
        :param save: absolute filepath and filename if figure should be saved
        :type show_faults: bool
        :param show_faults: plot active faults and hikurangi trench from
            internally saved coordinate files. takes extra time over simple plot
        :type figsize: tuple of floats
        :param figsize: length and width of the figure
        :type dpi: int
        :param dpi: dots per inch of the figure
        """
        from pyatoa.visuals.mapping import generate_map

        # Warn user if no invetnory is given
        if not isinstance(self.crate.inv, obspy.Inventory):
            warnings.warn("no inventory given, plotting blank map", UserWarning)

        # Set kew word arguments
        show = kwargs.get("show", True)
        save = kwargs.get("save", None)
        show_faults = kwargs.get("show_faults", False)
        annotate_names = kwargs.get("annotate_names", False)
        color_by_network = kwargs.get("color_by_network", False)
        map_corners = kwargs.get("map_corners",
                                 [-42.5007, -36.9488, 172.9998, 179.5077])

        figsize = kwargs.get("figsize", (8, 8.27))
        dpi = kwargs.get("dpi", 100)

        # Call external function to generate map
        generate_map(config=self.config, event_or_cat=self.crate.event,
                     inv=self.crate.inv, show_faults=show_faults,
                     annotate_names=annotate_names,
                     show=show, figsize=figsize, dpi=dpi, save=save,
                     color_by_network=color_by_network,
                     map_corners=map_corners
                     )





