"""
A class and associated functions that simplify calling Pyatoa functionality
with a SeisFlows workflow. Includes multiprocessing functionality to run Pyatoa
processing in parallel.
"""
import os
import pyatoa
import logging
import warnings
from glob import glob
from time import sleep
from copy import deepcopy
from pyasdf import ASDFDataSet
from pyatoa.utils.images import merge_pdfs
from pyatoa.utils.read import read_station_codes
from pyatoa.utils.asdf.clean import clean_dataset
from concurrent.futures import ProcessPoolExecutor


class IO(dict):
    """
    Dictionary with accessible attributes, used to simplify access to dicts.
    """
    def __init__(self, paths, logger, config, misfit=0, nwin=0, stations=0,
                 processed=0, exceptions=0, plot_fids=None):
        """
        Hard set required parameters here, that way the user knows what is
        expected of the IO class during the workflow.

        :type paths: pyatoa.core.pyaflowa.PathStructure
        :param paths: The specific path structure that Pyaflowa will use to
            navigate the filesystem, gather inputs and produce outputs.
        :type logger: logging.Logger
        :param logger: An individual event-specific log handler so that log
            statements can be made in parallel if required
        :type cfg: pyatoa.core.config.Config
        :param cfg: The event specific Config object that will be used to
            control the processing during each pyaflowa workflow.
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
        self.paths = paths
        self.logger = logger
        self.config = config
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


class PathStructure:
    """
    Generalizable path structure that Pyaflowa requires to work.
    The idea is that we hardcode paths into a separate class so that the
    functionality of Pyaflowa remains independent of the path structure,
    allowing Pyaflowa to operate e.g. with SeisFlows, or independently.
    """
    def __init__(self, structure="standalone", **kwargs):
        """
        Define the necessary path structure for Pyaflowa

        .. note::
            Pyaflowa mandates the following required directory structure:

            * cwd: The individual event working directory. This is modelled
              after a SPECFEM working directory, and for SeisFlows points
              directly to each of the solver working directories
            * datasets: The path where Pyaflowa is allowed to read and write
              ASDFDataSets which contain all the working data.
            * figures: Path where Pyaflowa is allowed to save the PDFS/PNGS
              that result from the workflow
            * logs: Path where Pyaflowa can store the log outputs from each
              of the individual event workflows
            * responses: The path to response files stored on disk in SEED fmt
              that the Gatherer object will search in order to find
              corresponding StationXML files. Can be left blank if StationXML
              files will be queried from FDSN
            * waveforms: Path to waveform files stored on disk in SEED format,
              same caveat as responses
            * synthetics: Paths to synthetic waveforms generated by SPECFEM.
              Need to be stored in directories pertaining to source names
              e.g. path/to/synthetics/SOURCE_NAME/*semd
            * stations_file: Path to the STATIONS file that dictates the
              receivers being used in the processing step. This file needs
              to match the SPECFEM3D format, and be saved into the source dir.
              e.g. path/to/SOURCE_NAME/STATIONS
            * adjsrcs: Path to save the resulting adjoint sources that will be
              generated during the processing workflow.

        :type structure: str
        :param structure: the choice of PathStructure

            * 'standalone': The default path structure that is primarily used
              for running Pyaflowa standalone, without other workflow tools.
            * 'seisflows': The path structure required when Pyaflowa is called
              by SeisFlows. Paths are hardcoded here based on the SeisFlows 
              directory structure.
        """
        # This 'constants' list mandates that the following paths exist.
        # The Pyaflowa workflow assumes that it can read/write from all of the
        # paths associated with these keys
        self._REQUIRED_PATHS = ["cwd", "datasets", "figures", "logs", "ds_file",
                                "stations_file", "responses", "waveforms",
                                "synthetics", "adjsrcs", "event_figures"]

        # Call the available function using its string representation,
        # which sets the internal path structure.
        try:
            getattr(self, structure)(**kwargs)
        except AttributeError as e:
            raise AttributeError(
                "{structure} is not a valid path structure") from e

    def __str__(self):
        """String representation for PathStructure, print out dict"""
        maxkeylen=max([len(_) for _ in self._REQUIRED_PATHS])

        str_out = ""
        for path in self._REQUIRED_PATHS:
            str_out += f"{path:<{maxkeylen}}: '{getattr(self, path)}'\n"
        return str_out
            
    def __repr__(self):
        """Simple call string representation"""
        return self.__str__()

    def standalone(self, workdir=None, datasets=None, figures=None,
                   logs=None, responses=None, waveforms=None, 
                   synthetics=None, adjsrcs=None, stations_file=None, **kwargs):
        """
        If Pyaflowa should be used in a standalone manner without external
        workflow management tools. Simply creates the necessary directory
        structure within the current working directory.
        Attributes can be used to overwrite the default path names
        """
        # General directories that all processes will write to
        self.workdir = workdir or os.getcwd()
        self.datasets = datasets or os.path.join(self.workdir, "datasets")
        self.figures = figures or os.path.join(self.workdir, "figures")
        self.logs = logs or os.path.join(self.workdir, "logs")

        # Event-specific directories that only certain processes will write to
        self.cwd = os.path.join(self.workdir, "{source_name}")
        self.ds_file = os.path.join(self.datasets, "{source_name}.h5")
        self.event_figures = os.path.join(self.figures, "{source_name}")
        self.synthetics = synthetics or os.path.join(self.workdir, "input",
                                                     "synthetics",
                                                     "{source_name}"
                                                     )

        # General read-only directories that may or may not contain input data
        self.responses = responses or os.path.join(self.workdir, "input", 
                                                   "responses")
        self.waveforms = waveforms or os.path.join(self.workdir, "input", 
                                                   "waveforms")

        # General write-only directories that processes will output data to
        self.adjsrcs = adjsrcs or os.path.join(self.workdir, "{source_name}", 
                                               "adjsrcs")

        # Event-specific read-only STATIONS file that defines stations to be
        # used during the workflow
        self.stations_file = stations_file or os.path.join(self.workdir, 
                                                           "{source_name}",
                                                           "STATIONS")

    def seisflows(self, **kwargs):
        """
        Hard coded SeisFlows directory structure which is created based on the
        PATH seisflows.config.Dict class, central to the SeisFlows workflow.
        """
        # Be explicit about the required argument 'sfpaths'
        PATH = kwargs.get("sfpaths", None)
        if PATH is None:
            raise TypeError("Pyaflowa SeisFlows path structure requires the "
                            "positional argument 'sfpaths' which should point "
                            "to the global PATH attribute in SeisFlows")

        self.workdir = PATH.WORKDIR
        self.cwd = os.path.join(PATH.SOLVER, "{source_name}")

        self.datasets = os.path.join(PATH.PREPROCESS, "datasets")
        self.figures = os.path.join(PATH.PREPROCESS, "figures")
        self.logs = os.path.join(PATH.PREPROCESS, "logs")
        
        self.ds_file = os.path.join(self.datasets, "{source_name}.h5")
        self.event_figures = os.path.join(self.figures, "{source_name}")

        self.responses = []
        self.waveforms = [os.path.join(self.cwd, "traces", "obs")]
        if PATH.DATA is not None: 
            self.responses.append(os.path.join(PATH.DATA, "seed"))
            self.waveforms.append(os.path.join(PATH.DATA, "mseed"))

        self.synthetics = [os.path.join(self.cwd, "traces", "syn")]
        self.adjsrcs = [os.path.join(self.cwd, "traces", "adj")]
        self.stations_file = os.path.join(self.cwd, "DATA", "STATIONS")

    def format(self, mkdirs=True, **kwargs):
        """
        Paths must be source dependent in order for the directory structure
        to remain dynamic and navigable. This function provides a blanket
        formatting to all required paths making it easier to pass this
        path structure around. Returns a copy of itself so that the internal
        structure is unchanged and can be re-used.

        :type mkdirs: bool
        :param mkdirs: make directories that don't exist. this should always be
            True otherwise Pyaflowa won't work as intended if directories are
            missing, but oh well it's here.
        :rtype: pyatoa.core.pyaflowa.PathStructure
        :return: a formatted PathStructure object
        """
        # Ensure we are not overwriting the template path structure
        path_structure_copy = deepcopy(self)

        for key in self._REQUIRED_PATHS:
            # Some paths may be lists so treat everything like a list
            req_paths = getattr(self, key)
            if isinstance(req_paths, str):
                req_paths = [req_paths]

            # Format using the kwargs, str with no format braces not affected
            fmt_paths = [os.path.abspath(_.format(**kwargs)) for _ in req_paths]
      
            # Files don't need to go through makedirs but need to exist. 
            if "_file" in key:
                # Dict comp to see which files in the list dont exist, if any
                file_bool = {f: os.path.exists(f) for f in fmt_paths 
                                                       if not os.path.exists(f)}
                # Kinda hacky way to skip over requiring dataset to exist,
                # maybe try to find a more elegant way to exclude it 
                if file_bool and key != "ds_file":
                    raise FileNotFoundError(f"Paths: {file_bool.keys()} "
                                            f"must exist and doesn't, please "
                                            f"check these files")

            # Required path structure must exist. Called repeatedly but cheap
            else:
                for path_ in fmt_paths:
                    if not os.path.exists(path_):
                        if mkdirs:
                            try:
                                os.makedirs(path_)  
                            except FileExistsError:
                                # Parallel processes trying to make the same dir
                                # will throw this error. Just let it pass as we
                                # only need to make them once.
                                continue
                        else:
                            raise IOError(f"{path_} is required but does not "
                                          f"exist and cannot be made")

            # Convert single lists back to str, ugh
            if len(fmt_paths) == 1:
                fmt_paths = fmt_paths[0]
            
            # Overwrite the template structure with the formatted one
            setattr(path_structure_copy, key, fmt_paths)

        return path_structure_copy


class Pyaflowa:
    """
    A class that simplifies calling the Pyatoa waveform misfit quantification
    workflow en-masse, i.e. processing, multiple stations and multiple events
    at once.
    """
    def __init__(self, structure="standalone", config=None, plot=True, 
                 map_corners=None, log_level="DEBUG", **kwargs):
        """
        Initialize the flow. Feel the flow.
        
        :type paths: seisflows.config.Dict
        :param paths: PREPROCESS module specific tasks that should be defined
            by the SeisFlows preprocess class. Three required keys, data,
            figures, and logs
        :type par: seisflows.config.Dict
        :param par: Parameter list tracked internally by SeisFlows
        """
        # Establish the internal workflow directories based on chosen structure
        self.structure = structure.lower()
        self.path_structure = PathStructure(self.structure, **kwargs)

        # Pyaflowa and Seisflows working together requires data exchange
        # of path and parameter information.
        if self.structure == "seisflows":
            sfpaths = kwargs.get("sfpaths")
            sfpar = kwargs.get("sfpar")

            assert(sfpaths is not None and sfpar is not None), \
                ("Pyaflowa + SeisFlows requires SeisFlows 'PAR' and 'PATH' "
                 "Dict objects to be passed to Pyaflowa upon initialization")

            # Pyatoa's Config object will initialize using the parameter already
            # set by the SeisFlows workflow. We need the BEGIN parameter to 
            # check status in window fixing.
            self.config = pyatoa.Config(seisflows_par=sfpar, **kwargs)
            self.begin = sfpar.BEGIN
        else:
            self.begin = -9999
            if config is None:
                warnings.warn("No Config object passed, initiating empty "
                              "Config", UserWarning)
                self.config = pyatoa.Config(iteration=1, step_count=0)
            else:
                self.config = config

        self.plot = plot
        self.map_corners = map_corners
        self.log_level = log_level

    def process_event(self, source_name, codes=None, **kwargs):
        """
        The main processing function for Pyaflowa misfit quantification.

        Processes waveform data for all stations related to a given event,
        produces waveform and map plots during the processing step, saves data
        to an ASDFDataSet and writes adjoint sources and STATIONS_ADJOINT file,
        required by SPECFEM3D's adjoint simulations, to disk.

        Kwargs passed to pyatoa.Manager.flow() function.

        :type source_name: str
        :param source_name: event id to be used for data gathering, processing
        :type codes: list of str
        :param codes: list of station codes to be used for processing. If None,
            will read station codes from the provided STATIONS file
        :rtype: float
        :return: the total scaled misfit collected during the processing chain
        """
        # Create the event specific configurations and attribute container (io)
        io = self.setup(source_name)
       
        # Allow user to provide a list of codes, else read from station file 
        if codes is None:
            codes = read_station_codes(io.paths.stations_file, 
                                       loc="??", cha="HH?")

        # Open the dataset as a context manager and process all events in serial
        with ASDFDataSet(io.paths.ds_file) as ds:
            mgmt = pyatoa.Manager(ds=ds, config=io.config)
            for code in codes:
                mgmt_out, io = self.process_station(mgmt=mgmt, code=code,
                                                    io=io, **kwargs)

        scaled_misfit = self.finalize(io)

        return scaled_misfit
        
    def multi_event_process(self, source_names, max_workers=None, **kwargs):
        """
        Use concurrent futures to run the process() function in parallel.
        This is a multiprocessing function, meaning multiple instances of Python
        will be instantiated in parallel.

        :type source_names: list of str
        :param solver_dir: a list of all the source names to process. each will
            be passed to process()
        :type max_workers: int
        :param max_workers: maximum number of parallel processes to use. If
            None, automatically determined by system number of processors.
        """
        misfits = {}
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for source_name, misfit in zip(
                    source_names, executor.map(self.process_event,
                                               source_names, kwargs)):
                misfits[os.path.basename(source_name)] = misfit

        return misfits

    def setup(self, source_name):
        """
        One-time basic setup to be run before each event processing step.
        Works by creating Config, logger and  establishing the necessary file 
        structure. Preps the ASDFDataSet before  processing writes to it, and 
        sets up the IO attribute dictionary to be carried around through the 
        processing procedure.

        ..note::
            IO object is not made an internal attribute because multiprocessing
            may require multiple, different IO objects to exist simultaneously,
            so they need to be passed into each of the functions.

        :type cwd: str
        :param cwd: current event-specific working directory within SeisFlows
        :rtype: pyatoa.core.pyaflowa.IO
        :return: dictionary like object that contains all the necessary
            information to perform processing for a single event
        """
        paths = self.path_structure.format(source_name=source_name)

        # Copy in the Config to avoid overwriting the template internal attr.
        config = deepcopy(self.config)

        # Set event-specific information so Pyatoa knows where to look for data
        config.event_id = source_name
        config.paths = {"responses": paths.responses,
                        "waveforms": paths.waveforms,
                        "synthetics": paths.synthetics
                        }

        # Only query FDSN at the very first function evaluation, assuming no new
        # data will pop up during the inversion
        if config.iteration != 1 and config.step_count != 0:
            config.client = None

        # Enure the ASDFDataSet has no previous data that may override current
        with ASDFDataSet(paths.ds_file) as ds:
            clean_dataset(ds, iteration=config.iteration, 
                          step_count=config.step_count) 
            config.write(write_to=ds)

        # Event-specific log files to track processing workflow
        log_fid = f"{config.iter_tag}{config.step_tag}_{config.event_id}.log"
        log_fid = os.path.join(paths.logs, log_fid)
        event_logger = self._create_event_log_handler(fid=log_fid)

        # Dict-like object used to keep track of information for a single event
        # processing run, simplifies information passing between functions.
        io = IO(paths=paths, logger=event_logger, config=config,
                misfit=0, nwin=0, stations=0, processed=0, exceptions=0,
                plot_fids=[])

        return io

    def finalize(self, io):
        """
        Wrapper for any finalization procedures after a single event workflow
        Returns total misfit calculated during process()

        :type io: pyatoa.core.pyaflowa.IO
        :param io: dict-like container that contains processing information
        :rtype: float or None
        :param: the scaled event-misfit, i.e. total raw misfit divided by
            number of windows. If no stations were processed, returns None 
            because that means theres a problem. If 0 were returned that would
            give the false impression of 0 misfit which is wrong.
        """
        self._make_event_pdf_from_station_pdfs(io)
        self._write_specfem_stations_adjoint_to_disk(io)
        self._output_final_log_summary(io)

        if io.misfit:
            return self._scale_raw_event_misfit(raw_misfit=io.misfit,
                                                nwin=io.nwin)
        else:
            return None

    def process_station(self, mgmt, code, io, **kwargs):
        """
        Process a single seismic station for a given event. Return processed
        manager and status describing outcome of processing. Multiple error 
        catching chunks to ensure that a failed processing for a single station
        won't kill the entire job. Needs to be called by process_event()

        Kwargs passed to pyatoa.core.manager.Manager.flow()

        .. note::
            Status used internally to track the processing success/failure rate
            * status == 0: Failed processing
            * status == 1: Successfully processed

        :type mgmt: pyatoa.core.manager.Manager
        :param mgmt: Manager object to be used for data gathering
        :type code: str
        :param code: Pyatoa station code, NN.SSS.LL.CCC
        :type io: pyatoa.core.pyaflowa.IO
        :param io: dict-like object that contains the necessary information
            to process the station
        :type fix_windows: bool
        :param fix_windows: pased to the Manager flow function, determines 
            whether previously gathered windows will be used to evaluate the 
            current set of synthetics. First passed through an internal check
            function that evaluates a few criteria before continuing.
        :rtype tuple: (pyatoa.core.manager.Manager, pyatoa.core.pyaflowa.IO)
        :return: a processed manager class, and the IO attribute class
        """
        net, sta, loc, cha = code.split(".")

        io.logger.info(f"\n{'=' * 80}\n\n{code}\n\n{'=' * 80}")
        io.stations += 1
        mgmt.reset()

        # Data gathering chunk; if fail, do not continue
        try:
            mgmt.gather(code=code)
        except pyatoa.ManagerError as e:
            io.logger.warning(e)
            return None, io

        # Data processing chunk; if fail, continue to plotting
        try:
            # Need to update fix window kwarg based on position in inversion
            kwargs = self._check_fix_windows(**kwargs)

            mgmt.flow(**kwargs)
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
                                 net, sta + ".pdf"]
                                )
            save = os.path.join(io.paths.event_figures, plot_fid)
            mgmt.plot(corners=self.map_corners, show=False, save=save)

            # If a plot is made, keep track so it can be merged later on
            io.plot_fids.append(save)

        # Finalization chunk; only if processing is successful
        if status == 1:
            # Keep track of outputs for the final log summary and misfit value
            io.misfit += mgmt.stats.misfit
            io.nwin += mgmt.stats.nwin
            io.processed += 1

            # SPECFEM wants adjsrcs for each comp, regardless if it has data
            mgmt.write_adjsrcs(path=io.paths.adjsrcs, write_blanks=True)

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

    def _check_fix_windows(self, fix_windows=False, **kwargs):
        """
        Determine how to address fixed time windows based on the user parameter
        as well as the current iteration and step count in relation to the
        inversion location.

        :type fix_windows: bool or str
        :param fix_windows: User-set parameter on whether to fix windows for this
            iteration/step count.
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
        :rtype: dict
        :return: kwargs updated with the fixed window criteria
        """
        # First function evaluation never fixes windows
        if self.config.iteration == 1 and self.config.step_count == 0:
            fix_windows_out = False
        elif isinstance(fix_windows, str):
            # By 'iter'ation only pick new windows on the first step count
            if fix_windows.upper() == "ITER":
                if self.config.step_count == 0:
                    fix_windows_out = False
                else:
                    fix_windows_out = True
            # 'Once' picks windows only for the first function evaluation of the
            # current set of iterations.
            elif fix_windows.upper() == "ONCE":
                if self.config.iteration == self.begin and \
                        self.config.step_count == 0:
                    fix_windows_out = False
                else:
                    fix_windows_out = True
        # Bool fix windows simply sets the parameter
        elif isinstance(fix_windows, bool):
            fix_windows_out = fix_windows
        else:
            raise NotImplementedError(f"Unknown choice {fix_windows} passed to "
                                      f"fixed_windows argument in manager flow")

        # Update kwargs to simplify calls
        kwargs["fix_windows"] = fix_windows_out

        return kwargs

    def _write_specfem_stations_adjoint_to_disk(self, io):
        """
        Create the STATIONS_ADJOINT file required by SPECFEM to run an adjoint
        simulation. Should be run after all processing has occurred. Works by
        checking what stations have adjoint sources available and re-writing the
        existing STATIONS file that is on hand.

        :type cwd: str
        :param cwd: current SPECFEM run directory within the larger SeisFlows
            directory structure
        """
        # These paths follow the structure of SeisFlows and SPECFEM
        adjoint_traces = glob(os.path.join(io.paths.adjsrcs, "*.adj"))

        # Simply append to end of file name e.g. "path/to/STATIONS" + "_ADJOINT"
        stations_adjoint = io.paths.stations_file  + "_ADJOINT"

        # Determine the network and station names for each adjoint source
        # Note this will contain redundant values but thats okay because we're
        # only going to check membership using it, e.g. [['NZ', 'BFZ']]
        adjoint_stations = [os.path.basename(_).split(".")[:2] for _ in
                            adjoint_traces]

        # The STATION file is already formatted so leverage for STATIONS_ADJOINT
        lines_in = open(io.paths.stations_file, "r").readlines()

        with open(stations_adjoint, "w") as f_out:
            for line in lines_in:
                # Station file line format goes: STA NET LAT LON DEPTH BURIAL
                # and we only need: NET STA
                check = line.split()[:2][::-1]
                if check in adjoint_stations:
                    f_out.write(line)

    def _make_event_pdf_from_station_pdfs(self, io):
        """
        Combine a list of single source-receiver PDFS into a single PDF file
        for the given event.

        :type fids: list of str
        :param fids: paths to the pdf file identifiers
        :type output_fid: str
        :param output_fid: name of the output pdf, will be joined to the figures
            path in this function
        """
        if io.plot_fids:
            # e.g. i01s00_2018p130600.pdf
            iterstep = f"{io.config.iter_tag}{io.config.step_tag}"
            output_fid = f"{iterstep}_{io.config.event_id}.pdf"

            # Merge all output pdfs into a single pdf, delete originals
            save = os.path.join(io.paths.event_figures, output_fid)
            merge_pdfs(fids=sorted(io.plot_fids), fid_out=save)

            for fid in io.plot_fids:
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
        # Propogate logging to individual log files, always overwrite
        handler = logging.FileHandler(fid, mode="w")
        # Maintain the same look as the standard console log messages
        logfmt = "[%(asctime)s] - %(name)s - %(levelname)s: %(message)s"
        formatter = logging.Formatter(logfmt, datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)

        for log in ["pyflex", "pyadjoint", "pyatoa"]:
            # Set the overall log level
            logger = logging.getLogger(log)
            logger.setLevel(self.log_level)
            logger.addHandler(handler)

        return logger

