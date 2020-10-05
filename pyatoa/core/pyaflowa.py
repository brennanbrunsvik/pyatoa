"""
A class and associated functions that simplify calling Pyatoa functionality
with a SeisFlows workflow. Includes multiprocessing functionality to run Pyatoa
processing in parallel.
"""
import os
import pyatoa
import logging
from glob import glob
from copy import deepcopy
from pyasdf import ASDFDataSet
from pyatoa.utils.asdf.clean import clean_dataset
from concurrent.futures import ProcessPoolExecutor


class IO(dict):
    """
    Dictionary with accessible attributes, used primarily for holding processing
    information inputs and outputs.
            # processing run, simplifies information passing between functions.
        io = IO(cwd=cwd, logger=logger_, inv=inv, misfit=0, nwin=0, stations=0,
                processed=0, exceptions=0, plot_fids=[])
    """
    def __init__(self, cwd, logger, inv, misfit=0, nwin=0, stations=0,
                 processed=0, exceptions=0, plot_fids=None):
        """
        Hard set required parameters here, that way the user knows what is
        expected of the IO class during the workflow.

        :type cwd: str
        :param cwd: The SeisFlows event-specific current working directory.
            This path should point to where a single SPECFEM run directory
            exists. Path is something like working_dir/scratch/solver/{event_id}
        :type logger: logging.Logger
        :param logger: An individual event-specific log handler so that log
            statements can be made in parallel if required
        :type inv: obspy.core.catalog.Catalog
        :param inv: The event specific Catalog object which needs to contain
            network and station information, used for looping through stations
            during the process() function
        :type misfit: int
        :param misfit: output storage to keep track of the total misfit accrued
            for all the stations processed for a given event
        :type nwin: int
        :param nwin: output storage to keep track of the total number of windows
            accrued during event processing. Will be used to scale raw misfit
        :type stations: int
        :param stations: output storage to keep track of the total number of
            stations where processing was attempted. For output log statement
        :type processed: int
        :param processed: output storage to keep track of the total number of
            stations where SUCCESSFULLY processed. For output log statement
        :type exceptions: int
        :param exceptions: output storage to keep track of the total number of
            stations where processing hit an unexpected exception.
            For output log statement
        :type plot_fids: list
        :param plot_fids: output storage to keep track of the the output .pdf
            files created for each source-receiver pair. Used to merge all pdfs
            into a single output pdf at the end of the processing workflow.
        """
        self.cwd = cwd
        self.logger = logger
        self.inv = inv
        self.misfit = misfit
        self.nwin = nwin
        self.stations = stations
        self.processed = processed
        self.exceptions = exceptions
        self.plot_fids = plot_fids or []

    def __setattr__(self, key, value):
        self[key] = value

    def __getattr__(self, key):
        return self[key]


class Pyaflowa:
    """
    A processing class for integration of the Pyatoa workflow into
    a SeisFlows Inversion workflow. Allows for multiprocessing of events.
    """
    def __init__(self, data, figures, par, loglevel="DEBUG"):
        """
        Initialize the flow by establishing the directory structures present
        in the SeisFlows workflow
        """
        # Information that needs to be passed in from SeisFlows.preprocess
        self.data = data
        self.figures = figures
        self.par = par

        self.plot = True
        self.corners = None
        self.loglevel = loglevel

        self.config = pyatoa.Config()
        self.config.read_seisflows_yaml(par=par)

    def setup(self, cwd):
        """
        Perform a basic setup by creating Config, logger and setting up an
        output dictionary to be carried around through the processing procedure.

        ..note::
            IO object is not made an internal attribute because multiprocessing
            may require multiple different IO objects to exist simultaneously,
            so they need to be passed into each of the functions.

        :type cwd: str
        :param cwd: current event-specific working directory within SeisFlows
        :rtype: pyatoa.core.pyaflowa.IO
        :return: dictionary like object that contains all the necessary
            information to perform processing for a single event
        """
        # Get the event id from the current working directory
        event_id = os.path.basename(cwd)

        # Copy in the Config to avoid overwriting the template internal attr.
        config = deepcopy(self.config)
        config.event_id = event_id

        # Create the individualized logger
        logger_ = self._create_(fid=f"{config.event_id}.log")

        # Reading in stations here allows for event-dependent station lists
        inv = pyatoa.read_station(os.path.join(cwd, "DATA", "STATIONS"))

        # Dict-like object used to keep track of information for a single event
        # processing run, simplifies information passing between functions.
        io = IO(cwd=cwd, logger=logger_, inv=inv, misfit=0, nwin=0, stations=0,
                processed=0, exceptions=0, plot_fids=[])

        return io

    def finalize(self, io):
        """
        Wrapper for any finalization procedures after a single event workflow
        Returns total misfit calculated during process()

        :type io: pyatoa.core.pyaflowa.IO
        :param io: dict-like container that contains processing information
        """
        self._make_event_pdf_from_station_pdfs(
            fids=io.plot_fids,
            output_fid="_".join([io.config.iter_tag, io.config.step_tag,
                                 io.config.event_id + ".pdf"])
        )
        self._write_stations_adjoint_to_disk(io.cwd)
        self._output_final_log_summary(io)

        if io.misfit:
            return self._scale_raw_event_misfit(raw_misfit=io.misfit,
                                                nwin=io.nwin)
        else:
            return None

    def process(self, cwd, **kwargs):
        """
        A template processing function to create/open an ASDFDataSet, process
        all stations related to the event, and write the associated adjoint
        sources to disk. Kwargs passed to Manager.flow()

        :rtype: float
        :return: the total scaled misfit collected during the processing chain
        """
        # Create the event specific configurations and output containers
        io = self.setup(cwd)

        # Open the dataset as a context manager and process all events serially
        with ASDFDataSet(os.path.join(self.data,
                                      f"{io.config.event_id}")) as ds:
            # Cleaning dataset ensures no previous/failed data is used this go
            clean_dataset(ds, iteration=io.config.iteration,
                          step_count=io.config.step_count
                          )
            io.config.write(write_to=ds)

            mgmt = pyatoa.Manager(ds=ds, config=io.config)
            for net in io.inv:
                for sta in net:
                    code = f"{net.code}.{sta.code}"
                    mgmt_out, io = self.quantify(mgmt, code, io, **kwargs)

        return self.finalize(io)

    def multi_process(self, solver_dir, max_workers=None, **kwargs):
        """
        Use multiprocessing to run event processing functionality in parallel.
        Max workers is intentionally left blank so that it can be automatically
        determined by the number of processors.
        """
        source_paths = glob(os.path.join(solver_dir, "*"))

        # Do not consider symlinks such as 'mainsolver'
        source_paths = [_ for _ in source_paths if not os.path.islink(_)]

        misfits = {}
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for source_path, misfit in zip(
                    source_paths, executor.map(self.process_single_event,
                                               source_paths)):
                misfits[os.path.basename(source_path)] = misfit

        return misfits

    def quantify(self, mgmt, code, io, **kwargs):
        """
        Process a single seismic station for a given event. Return processed
        manager and status describing outcome of processing.
        Kwargs passed to pyatoa.core.manager.Manager.flow()

        ..note::
            Status used internally to track the processing success/failure rate
            * status == 0: Failed processing
            * status == 1: Successfully processed

        :type mgmt: pyatoa.core.manager.Manager
        :param mgmt: Manager object to be used for data gathering
        :type code: str
        :param code: network and station code joined by '.', e.g. 'NZ.BFZ'
        :type io: pyatoa.core.pyaflowa.IO
        :param io: dict-like object that contains the necessary information
            to process the station
        :rtype tuple: (pyatao.core.manager.Manager, str, int)
        :return: a processed manager class, the output filename of the resulting
            pdf image, and an integer describing the status of the processing.
            Failures will lead to
        """
        io.logger.info(f"\n{'=' * 80}\n\n{code}\n\n{'=' * 80}")
        io.stations += 1
        mgmt.reset()

        # Data gathering chunk; if fail, do not continue
        try:
            mgmt.gather(code=f"{code}.*.HH?")
        except pyatoa.ManagerError as e:
            io.logger.warning(e)
            return None, None, 0

        # Data processing chunk; if fail, continue to plotting
        try:
            mgmt.flow(fix_windows=self._check_fixed_windows(), **kwargs)
            status = 1
        except pyatoa.ManagerError as e:
            io.logger.warning(e)
            status = 0
            pass
        except Exception as e:
            # Uncontrolled exceptions should be noted in more detail
            io.logger.warning(e, exc_info=True)
            io.exceptions += 1
            status = 0
            pass

        # Plotting chunk; fid is e.g. path/i01s00_NZ_BFZ.pdf
        if self.plot:
            plot_fid = "_".join([mgmt.config.iter_tag, mgmt.config.step_tag,
                                 code.replace(".", "_") + ".pdf"]
                                )
            save = os.path.join(self.figures, mgmt.config.event_id,
                                plot_fid)
            mgmt.plot(corners=self.corners, show=False, save=save)
        else:
            save = None

        # Keep track of outputs for the log summary and figures
        if status == 1:
            # SPECFEM wants adjsrcs for each comp, regardless if it has data
            mgmt.write_adjsrcs(path=os.path.join(io.cwd, "traces", "adj"),
                               write_blanks=True)

            io.plot_fids.append(save)
            io.misfit += mgmt.stats.misfit
            io.nwin += mgmt.stats.nwin
            io.processed += 1

        return mgmt, io

    @staticmethod
    def _scale_raw_event_misfit(raw_misfit, nwin):
        """
        Scale event misfit based on event misfit equation defined by
        Tape et al. (2010)

        :type raw_misfit: float
        :param raw_misfit: the total summed misfit from a processing chain
        :type nwin: int
        param nwin: number of windows collected during processing
        :rtype: float
        :return: scaled event misfit
        """
        return 0.5 * raw_misfit / nwin

    def _check_fixed_windows_criteria(self):
        """
        Determine how to address fixed time windows based on the user parameter
        as well as the current iteration and step count in relation to the
        inversion location.

        Options:
            True: Always fix windows except for i01s00 because we don't have any
                  windows for the first function evaluation
            False: Don't fix windows, always choose a new set of windows
            Iter: Pick windows only on the initial step count (0th) for each
                  iteration. WARNING - does not work well with Thrifty Inversion
                  because the 0th step count is usually skipped
            Once: Pick new windows on the first function evaluation and then fix
                  windows. Useful for when parameters have changed, e.g. filter
                  bounds

        :rtype: bool
        :return: flag to denote whether or not to pick new windows in the
            processing workflow
        """
        # User-defined choice for window cofixing
        choice = self.par.FIX_WINDOWS

        # First function evaluation never fixes windows
        if self.config.iteration == 1 and self.config.step_count == 0:
            fix_windows = False
        elif isinstance(choice, str):
            # By 'iter'ation only pick new windows on the first step count
            if choice.upper() == "ITER":
                if self.config.step_count == 0:
                    fix_windows = False
                else:
                    fix_windows = True
            # 'Once' picks windows only for the first function evaluation of the
            # current set of iterations.
            elif choice.upper() == "ONCE":
                if self.config.iteration == self.par.BEGIN and \
                        self.config.step_count == 0:
                    fix_windows = False
                else:
                    fix_windows = True
        # Bool fix windows simply sets the parameter
        else:
            fix_windows = self.par.FIX_WINDOWS

        return fix_windows

    @staticmethod
    def _write_stations_adjoint_to_disk(cwd):
        """
        Create the STATIONS_ADJOINT file required by SPECFEM to run an adjoint
        simulation. Should be run after all processing has occurred. Does this
        by checking what stations have adjoint sources available.

        :type cwd: str
        :param cwd: current SPECFEM run directory within the larger SeisFlows
            directory structure
        """
        # These paths follow the structure of SeisFlows and SPECFEM
        adjoint_traces = glob(os.path.join(cwd, "traces", "adj", "*.adj"))
        stations = os.path.join(cwd, "DATA", "STATIONS")
        stations_adjoint = os.path.join(cwd, "DATA", "STATIONS_ADJOINT")

        # Determine the network and station names for each adjoint source
        # Note this will contain redundant values but thats okay because we're
        # only going to check membership using it, e.g. [['NZ', 'BFZ']]
        adjoint_stations = [os.path.basename(_).split(".")[:2] for _ in
                            adjoint_traces]

        # The STATION file is already formatted so leverage for STATIONS_ADJOINT
        lines_in = open(stations, "r").readlines()

        with open(stations_adjoint, "w") as f_out:
            for line in lines_in:
                # Station file line format goes: STA NET LAT LON DEPTH BURIAL
                # and we only need: NET STA
                check = line.split()[:2][::-1]
                if check in adjoint_stations:
                    f_out.write(line)

    def _make_event_pdf_from_station_pdfs(self, fids, output_fid):
        """
        Combine a list of single source-receiver PDFS into a single PDF file
        for the given event.

        :type fids: list of str
        :param fids: paths to the pdf file identifiers
        :type output_fid: str
        :param output_fid: name of the output pdf, will be joined to the figures
            path in this function
        """
        if fids:
            # Merge all output pdfs into a single pdf, delete originals
            save = os.path.join(self.figures, output_fid)
            pyatoa.utils.images.merge_pdfs(fids=sorted(fids), fid_out=save)
            for fid in fids:
                os.remove(fid)

    def _output_final_log_summary(self, io):
        """
        Write summary information at the end of a workflow to the log file

        :type io: pyatoa.core.pyaflowa.IO
        :param io: dict-like container that contains processing information
        """
        io.logger.info(f"\n{'=' * 80}\n\nSUMMARY\n\n{'=' * 80}\n"
                       f"SOURCE NAME: {io.config.event_id}\n"
                       f"STATIONS: {io.processed} / {io.stations}\n"
                       f"WINDOWS: {io.nwin}\n"
                       f"RAW MISFIT: {io.misfit:.2f}\n"
                       f"UNEXPECTED ERRORS: {io.exceptions}"
                       )

    def _create_event_log_handler(self, fid):
        """
        Create a separate log file for each multiprocessed event.

        :type fid: str
        :param fid: the name of the outputted log file
        :rtype: logging.Logger
        :return: an individualized logging handler
        """
        for log in ["pyatoa", "pyflex", "pyadjoint"]:
            # Set the overall log level
            logger = logging.getLogger(log)
            logger.setLevel(self.loglevel)
            # Propogate logging to individual log files
            handler = logging.FileHandler(fid)
            # Maintain the same look as the standard console log messages
            logfmt = "[%(asctime)s] - %(name)s - %(levelname)s: %(message)s"
            formatter = logging.Formatter(logfmt, datefmt="%Y-%m-%d %H:%M:%S")
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

