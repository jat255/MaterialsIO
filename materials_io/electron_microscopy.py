from hyperspy.io import load as hs_load
import re
import logging

from materials_io.base import BaseSingleFileParser
from materials_io.utils import get_nested_dict_value_by_path as get_val
from materials_io.utils import map_dict_values, MappingElements
from materials_io.utils import set_nested_dict_value_with_units as set_val_units

from typing import Tuple, Dict, Optional

logger = logging.getLogger(__name__)

class ElectronMicroscopyParser(BaseSingleFileParser):
    """Parse metadata specific to electron microscopy, meaning any file
    supported by HyperSpy's I/O capabilities. Extract both the metadata
    interpreted by HyperSpy directly, but also any important values we can
    pick out manually.

    For each value (if known), return the subdict:
      {
        "value": numeric/str value,
        "unit": unit name from http://qudt.org/vocab/unit/ (see 
                http://www.qudt.org/doc/DOC_VOCAB-UNITS.html for details)
      }
    
    For base EM, return the following under "General_EM"
      - X accelerating_voltage
      - X acquisition_mode
      - X acquisition_software_name
      - X acquisition_software_version
      - X beam_current (measured at the sample)
      - X beam_energy 
      - X convergence_angle
      - X detector_name
      - X dwell_time (for STEM modes)
      - X emission_current
      - X exposure_time (for non-STEM modes)
      - X elements (list of str - as detected from spectroscopy signal)
      - X magnification_actual
      - X magnification_indicated
      - X microscope_name
      - X probe_area
      - X stage_position (dict with X, Y, Z, R, etc. as required)

    For all, return the following under "General" (if known, from HyperSpy,
      following their metadata definitions:
      http://hyperspy.org/hyperspy-doc/current/user_guide/metadata_structure
      .html)
     - authors : str 
       - The authors of the data, in Latex format: Surname1, Name1 and
         Surname2, Name2, etc.
     - date : str
       - The acquisition or creation date in ISO 8601 date format,
         e.g. ‘2018-01-28’
     - doi : str
       - Digital object identifier of the data, e.g. doi:10.5281/zenodo.58841
     - original_filename : str
       - If the signal was loaded from a file this key stores the name of the
         original file
     - notes : str
       - Notes about the data
     - time : str
       - The acquisition or creation time in ISO 8601 time format,
         e.g. ‘13:29:10’
     - time_zone : str
       - The time zone in either tzdata form, e.g. “UTC”, “America/New_York”,
         etc., or a time offset, e.g. “+03:00” or “-05:00”
     - title : str
       - A title for the signal, e.g. “Sample overview”

    For TEM:
      - X acquisition_device
      - X acquisition_format
      - X acquisition_mode
      - X acquisition_signal
      - X extractor_voltage
      - X illumination_mode
      - X imaging_mode
      - X operation_mode
      - X spherical_aberration_coefficient
      - X defocus
      - X spot_size
      - X camera_length

    For SEM:
      - chamber_pressure
      - horizontal_field_width
      - magnification_mode
      - pixel_height
      - pixel_width
      - frame_time
      - working_distance
      - vertical_field_width
      
    For EELS:
      - XX aperture_size
      - XX collection_angle
      - X dispersion_per_channel
      - X drift_tube_voltage
      - X drift_tube_enabled
      - X energy_loss_offset
      - X filter_slit_width
      - X filter_slit_inserted
      - X integration_time
      - X number_of_samples (The number of frames/spectra integrated during the
                             acquisition.)
      - X prism_shift_voltage
      - X prism_shift_enabled
      - X spectrometer_mode
      - XX spectrometer_name
      - X total_energy_loss

    For EDS:
      - X azimuth_angle
      - X detector_type
      - X dispersion_per_channel
      - X elevation_angle
      - X energy_resolution_MnKa
      - X incidence_angle
      - X live_time
      - X real_time
      - X solid_angle
      - X stage_tilt

    For all:
      - raw_metadata - the superset of whatever metadata was extracted from
        the original file
    """

    def _parse_file(self, file_path: str, context: Dict = None) -> Dict:
        self.em = {}
        self.image = {}
        self.inst_data = None

        # Read file lazily (reduce memory), both HyperSpy-formatted and raw data
        hs_data = hs_load(file_path, lazy=True)

        # if hs_data is a list, pull out first for metadata extraction
        if isinstance(hs_data, list):
            hs_data = hs_data[0]

        self.meta = hs_data.metadata.as_dictionary()
        self.raw_meta = hs_data.original_metadata.as_dictionary()
        self.em['raw_metadata'] = self.raw_meta

        for s in ['General', 'General_EM', 'TEM', 'SEM', 'EDS', 'EELS']:
            self.em[s] = {}

        # call each individual processor
        self._process_hs_data()
        self._dm3_general_info()
        self._dm3_eels_info()
        self._dm3_tecnai_info()
        self._dm3_eds_info()

        # TODO:
        self._dm3_spectrum_image_info()

        self._tia_info()  # ...and so on
        self._tiff_info()  # ...and so on

        # Non-HS data (not pulled into standard HS metadata)
        # Pull out common dicts
        try:
            micro_info = self.raw_meta["ImageList"]["TagGroup0"]["ImageTags"][
                "Microscope Info"]
        except Exception:
            micro_info = {}
        try:
            exp_desc = self.raw_meta["ObjectInfo"]["ExperimentalDescription"]
        except Exception:
            exp_desc = {}

        # emission_current
        try:
            self.em["emission_current"] = float(micro_info["Emission Current (µA)"])
        except Exception:
            try:
                self.em["emission_current"] = float(exp_desc["Emission_uA"])
            except Exception:
                pass
        # operation_mode
        try:
            self.em["operation_mode"] = str(micro_info["Operation Mode"])
        except Exception:
            pass
        # microscope
        try:
            self.em["microscope"] = str(self.raw_meta["ImageList"]["TagGroup0"]["ImageTags"]
                                           ["Session Info"]["Microscope"])
        except Exception:
            try:
                self.em["microscope"] = str(micro_info["Name"])
            except Exception:
                pass
        # spot_size
        try:
            self.em["spot_size"] = int(exp_desc["Spot size"])
        except Exception:
            pass

        # Image metadata
        try:
            shape = []
            base_shape = [int(dim) for dim in self.raw_meta["ImageList"]["TagGroup0"]
                                                      ["ImageData"]["Dimensions"].values()]
            # Reverse X and Y order to match MDF schema (y, x, z, ..., channels)
            if len(base_shape) >= 2:
                shape.append(base_shape[1])
                shape.append(base_shape[0])
                shape.extend(base_shape[2:])
            # If 1 dimension, don't need to swap
            elif len(base_shape) > 0:
                shape = base_shape

            if shape:
                self.image["shape"] = shape
        except Exception as e:
            print(e)
            pass

        # Remove None/empty values
        for key, val in list(self.em.items()):
            if val is None or val == [] or val == {}:
                self.em.pop(key)

        record = {}
        if self.em:
            record["electron_microscopy"] = self.em
        if self.image:
            record["image"] = self.image

        return record

    def _process_hs_data(self) -> None:
        # Image mode is SEM, TEM, or STEM
        # STEM is a subset of TEM
        if "SEM" in self.meta.get('Acquisition_instrument', {}).keys():
            self.inst = "SEM"
        elif "TEM" in self.meta.get('Acquisition_instrument', {}).keys():
            self.inst = "TEM"
        else:
            self.inst = 'None'

        # HS data
        self.inst_data = get_val(self.meta, ('Acquisition_instrument',
                                             self.inst))
        if self.inst_data is not None:
            source = self.inst_data
            dest = self.em
            mapping = [
                MappingElements(
                    source_dict=source, source_path='acquisition_mode',
                    dest_dict=dest, dest_path=('General_EM',
                                                  'acquisition_mode'),
                    cast_fn=str, units=None, conv_fn=None, override=False),
                MappingElements(
                    source_dict=source, source_path='beam_current',
                    dest_dict=dest, dest_path=('General_EM', 'beam_current'),
                    cast_fn=float, units='NanoA', conv_fn=None, override=False),
                MappingElements(
                    source_dict=source, source_path='beam_energy',
                    dest_dict=dest, dest_path=('General_EM', 'beam_energy'),
                    cast_fn=float, units='KiloEV', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source, source_path='convergence_angle',
                    dest_dict=dest, dest_path=('General_EM',
                                               'convergence_angle'),
                    cast_fn=float, units='MilliRAD', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source, source_path='magnification',
                    dest_dict=dest, dest_path=('General_EM',
                                               'magnification_indicated'),
                    cast_fn=float, units='UNITLESS', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source, source_path='microscope',
                    dest_dict=dest, dest_path=('General_EM', 'microscope_name'),
                    cast_fn=str, units=None, conv_fn=None, override=False),
                MappingElements(
                    source_dict=source, source_path='probe_area',
                    dest_dict=dest, dest_path=('General_EM', 'probe_area'),
                    cast_fn=float, units='NanoM2', conv_fn=None,
                    override=False),

                # stage positions
                MappingElements(
                    source_dict=source,
                    source_path=('Stage', 'rotation'),
                    dest_dict=dest,
                    dest_path=('General_EM', 'stage_position', 'rotation'),
                    cast_fn=float, units='DEG', conv_fn=None, override=False),
                MappingElements(
                    source_dict=source,
                    source_path=('Stage', 'tilt_alpha'),
                    dest_dict=dest,
                    dest_path=('General_EM', 'stage_position', 'tilt_alpha'),
                    cast_fn=float, units='DEG', conv_fn=None, override=False),
                MappingElements(
                    source_dict=source,
                    source_path=('Stage', 'tilt_beta'),
                    dest_dict=dest,
                    dest_path=('General_EM', 'stage_position', 'tilt_beta'),
                    cast_fn=float, units='DEG', conv_fn=None, override=False),
                MappingElements(
                    source_dict=source,
                    dest_dict=dest,
                    source_path=('Stage', 'x'),
                    dest_path=('General_EM', 'stage_position', 'x'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source,
                    source_path=('Stage', 'y'),
                    dest_dict=dest,
                    dest_path=('General_EM', 'stage_position', 'y'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source,
                    source_path=('Stage', 'z'),
                    dest_dict=dest,
                    dest_path=('General_EM', 'stage_position', 'z'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False),

                # camera length/working distance
                MappingElements(
                    source_dict=source, source_path='camera_length',
                    dest_dict=dest, dest_path=('TEM', 'camera_length'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=source, source_path='working_distance',
                    dest_dict=dest, dest_path=('SEM', 'working_distance'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False)]

            map_dict_values(mapping)

            self._process_hs_detectors()

        source = self.meta
        dest = self.em
        mapping = [
            # Elements present (if known)
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('Sample', 'elements'),
                dest_path=('General_EM', 'elements'),
                cast_fn=list, units=None, conv_fn=None, override=False),
            # General metadata
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'date'),
                dest_path=('General', 'date'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'doi'),
                dest_path=('General', 'doi'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'original_filename'),
                dest_path=('General', 'original_filename'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'notes'),
                dest_path=('General', 'notes'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'time'),
                dest_path=('General', 'time'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'time_zone'),
                dest_path=('General', 'time_zone'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=source, dest_dict=dest,
                source_path=('General', 'title'),
                dest_path=('General', 'title'),
                cast_fn=str, units=None, conv_fn=None, override=False)]

        map_dict_values(mapping)

    def _process_hs_detectors(self) -> None:
        """
        Parses HyperSpy-formatted metadata specific to detectors as specified by
        http://hyperspy.org/hyperspy-doc/current/user_guide
        /metadata_structure.html
        """
        detector_node = get_val(self.inst_data, 'Detector')
        dest_dict = self.em
        mapping = [
            MappingElements(
                source_dict=self.inst_data, source_path='detector_type',
                dest_dict=dest_dict, dest_path=('General_EM', 'detector_name'),
                cast_fn=str, units=None, conv_fn=None, override=False
            )
        ]

        if detector_node is not None:
            mapping += [
                # EDS
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EDS', 'azimuth_angle'),
                    dest_dict=dest_dict, dest_path=('EDS', 'azimuth_angle'),
                    cast_fn=float, units='DEG', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EDS', 'elevation_angle'),
                    dest_dict=dest_dict, dest_path=('EDS', 'elevation_angle'),
                    cast_fn=float, units='DEG', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EDS', 'energy_resolution_MnKa'),
                    dest_dict=dest_dict,
                    dest_path=('EDS', 'energy_resolution_MnKa'),
                    cast_fn=float, units='EV', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node, source_path=('EDS', 'live_time'),
                    dest_dict=dest_dict, dest_path=('EDS', 'live_time'),
                    cast_fn=float, units='SEC', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node, source_path=('EDS', 'real_time'),
                    dest_dict=dest_dict, dest_path=('EDS', 'real_time'),
                    cast_fn=float, units='SEC', conv_fn=None, override=False),

                # EELS
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'aperture_size'),
                    dest_dict=dest_dict, dest_path=('EELS', 'aperture_size'),
                    cast_fn=float, units='MilliM', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'collection_angle'),
                    dest_dict=dest_dict, dest_path=('EELS', 'collection_angle'),
                    cast_fn=float, units='MilliRAD', conv_fn=None,
                    override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'dwell_time'),
                    dest_dict=dest_dict, dest_path=('General_EM', 'dwell_time'),
                    cast_fn=float, units='SEC', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'exposure'),
                    dest_dict=dest_dict,
                    dest_path=('General_EM', 'exposure_time'),
                    cast_fn=float, units='SEC', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'frame_number'),
                    dest_dict=dest_dict,
                    dest_path=('EELS', 'number_of_samples'),
                    cast_fn=int, units='NUM', conv_fn=None, override=False),
                MappingElements(
                    source_dict=detector_node,
                    source_path=('EELS', 'spectrometer'),
                    dest_dict=dest_dict,
                    dest_path=('EELS', 'spectrometer_name'),
                    cast_fn=str, units=None, conv_fn=None, override=False),
            ]
        map_dict_values(mapping)

    def _dm3_general_info(self) -> None:
        """Parse commonly-found TEM-related tags in DigitalMicrograph files"""
        # process "Microscope Info"
        base = self.__get_dm3_tag_pre_path() + ('Microscope Info',)
        dest_dict = self.em
        mapping = [
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Indicated Magnification',),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'magnification_indicated'),
                cast_fn=float, units='UNITLESS', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Actual Magnification',),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'magnification_actual'),
                cast_fn=float, units='UNITLESS', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Cs(mm)',),
                dest_dict=dest_dict,
                dest_path=('TEM', 'spherical_aberration_coefficient'),
                cast_fn=float, units='MilliM', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('STEM Camera Length',),
                dest_dict=dest_dict,
                dest_path=('TEM', 'camera_length'),
                cast_fn=float, units='MilliM', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Operation Mode',),
                dest_dict=dest_dict, dest_path=('TEM', 'operation_mode'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Imaging Mode',),
                dest_dict=dest_dict, dest_path=('TEM', 'imaging_mode'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Illumination Mode',),
                dest_dict=dest_dict, dest_path=('TEM', 'illumination_mode'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Microscope',),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'microscope_name'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Stage Position', 'Stage X'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'stage_position', 'x'),
                cast_fn=float, units='MilliM', conv_fn=lambda x: x/1000,
                override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Stage Position', 'Stage Y'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'stage_position', 'y'),
                cast_fn=float, units='MilliM', conv_fn=lambda x: x / 1000,
                override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Stage Position', 'Stage Z'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'stage_position', 'z'),
                cast_fn=float, units='MilliM', conv_fn=lambda x: x / 1000,
                override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Stage Position', 'Stage Alpha'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'stage_position', 'tilt_alpha'),
                cast_fn=float, units='DEG', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Stage Position', 'Stage Beta'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'stage_position', 'tilt_beta'),
                cast_fn=float, units='DEG', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Emission Current (µA)',),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'emission_current'),
                cast_fn=float, units='MicroA', conv_fn=None, override=False),
        ]

        voltage = get_val(self.raw_meta, base + ('Voltage',), float)
        if voltage is not None:
            mapping += [
                MappingElements(
                    source_dict=self.raw_meta, dest_dict=dest_dict,
                    source_path=base + ('Voltage',), cast_fn=float,
                    dest_path=('General_EM', 'accelerating_voltage'),
                    units='KiloEV' if voltage >= 1000 else 'EV',
                    conv_fn=lambda x: x / 1000 if voltage >= 1000 else x,
                    override=False)
            ]

        # "Session Info"
        base = self.__get_dm3_tag_pre_path() + ('Session Info',)
        mapping += [
            MappingElements(
                source_dict=self.raw_meta, source_path=base + ('Detector',),
                dest_dict=dest_dict, dest_path=('General_EM', 'detector_name'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta, source_path=base + ('Microscope',),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'microscope_name'),
                cast_fn=str, units=None, conv_fn=None, override=False)]

        # "Meta Data"
        base = self.__get_dm3_tag_pre_path() + ('Meta Data',)
        mapping += [
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Acquisition Mode',),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_mode'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Format',),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_format'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Signal',),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_signal'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            # sometimes the EDS signal label is in a different place
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Experiment keywords', 'TagGroup1',
                                    'Label'),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_signal'),
                cast_fn=str, units=None, conv_fn=None, override=False)
        ]

        # a few miscellaneous DM tags:
        base = self.__get_dm3_tag_pre_path()
        mapping += [
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Acquisition', 'Device', 'Name'),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_device'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('DataBar', 'Device Name'),
                dest_dict=dest_dict, dest_path=('TEM', 'acquisition_device'),
                cast_fn=str, units=None, conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('Acquisition', 'Parameters', 'High Level',
                                    'Exposure (s)'),
                dest_dict=dest_dict, dest_path=('General_EM', 'exposure_time'),
                cast_fn=float, units='SEC', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('DataBar', 'Exposure Time (s)'),
                dest_dict=dest_dict, dest_path=('General_EM', 'exposure_time'),
                cast_fn=float, units='SEC', conv_fn=None, override=False),
            MappingElements(
                source_dict=self.raw_meta,
                source_path=base + ('GMS Version', 'Created'),
                dest_dict=dest_dict,
                dest_path=('General_EM', 'acquisition_software_version'),
                cast_fn=str, units=None, conv_fn=None, override=False)
        ]

        if get_val(self.raw_meta, ('ImageList', 'TagGroup0', 'ImageTags')) is\
                not None:
            # we have DigitalMicrograph tags, so set acquisition software name
            set_val_units(nest_dict=dest_dict,
                          path=('General_EM', 'acquisition_software_name'),
                          value='DigitalMicrograph')

        map_dict_values(mapping)

    def __get_dm3_tag_pre_path(self) -> Tuple:
        """
        Get the path into a dictionary where the important DigitalMicrograph
        metadata is expected to be found. If the .dm3/.dm4 file contains a stack
        of images, the metadata to extract is instead under a `plane info`
        tag, so this method will determine if the stack metadata is present
        and return the correct path. ``pre_path`` will be something
        like ``('ImageList', 'TagGroup0', 'ImageTags', 'plane info',
        'TagGroup0', 'source tags')``.

        Returns
        -------
        pre_path
            A tuple containing the subsequent keys that need to be traversed to
            get to the point in the ``raw_metadata`` where the important
            metadata is stored
        """
        # test if we have a stack
        stack_path = ('ImageList', 'TagGroup0', 'ImageTags', 'plane info')
        stack_val = get_val(self.raw_meta, stack_path)
        if stack_val is not None:
            # we're in a stack
            pre_path = ('ImageList', 'TagGroup0', 'ImageTags', 'plane info',
                        'TagGroup0', 'source tags')
        else:
            pre_path = ('ImageList', 'TagGroup0', 'ImageTags')

        return pre_path

    def _dm3_eels_info(self) -> None:
        # basic EELS metadata
        pre_path = self.__get_dm3_tag_pre_path()
        base = pre_path + ('EELS', )
        mapping = [
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Acquisition', 'Exposure (s)'),
                dest_path=('General_EM', 'exposure_time'), units='SEC',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Acquisition', 'Integration time (s)'),
                dest_path=('EELS', 'integration_time'), units='SEC',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Acquisition', 'Number of frames'),
                dest_path=('EELS', 'number_of_samples'), units='NUM',
                conv_fn=None, cast_fn=int, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Experimental Conditions',
                                    'Collection semi-angle (mrad)'),
                dest_path=('EELS', 'collection_angle'), units='MilliRAD',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Experimental Conditions',
                                    'Convergence semi-angle (mrad)'),
                dest_path=('General_EM', 'convergence_angle'), units='MilliRAD',
                conv_fn=None, cast_fn=float, override=False)]

        # spectrometer metadata
        # is usually at one of two places, so try both
        spect_dict = get_val(self.raw_meta,
                             pre_path + ('EELS', 'Acquisition', 'Spectrometer'))
        if spect_dict is not None:
            spect_path = pre_path + ('EELS', 'Acquisition', 'Spectrometer')
        else:
            spect_path = pre_path + ('EELS Spectrometer',)
        mapping += [
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Aperture label',),
                dest_path=('EELS', 'aperture_size'), units='MilliM',
                conv_fn=None, cast_fn=lambda s: float(s.replace(' mm', '')),
                override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Dispersion (eV/ch)',),
                dest_path=('EELS', 'dispersion_per_channel'), units='EV',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Energy loss (eV)',),
                dest_path=('EELS', 'energy_loss_offset'), units='EV',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Instrument name',),
                dest_path=('EELS', 'spectrometer_name'), units=None,
                conv_fn=None, cast_fn=str, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Drift tube voltage (V)',),
                dest_path=('EELS', 'drift_tube_voltage'), units='V',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Drift tube enabled',),
                dest_path=('EELS', 'drift_tube_enabled'), units=None,
                conv_fn=None, cast_fn=bool, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Prism offset (V)',),
                dest_path=('EELS', 'prism_shift_voltage'), units='V',
                conv_fn=None, cast_fn=float, override=False),
            # note space at end of "Prism offset enabled " because that's how
            # it gets loaded in from DigitalMicrograph...
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Prism offset enabled ',),
                dest_path=('EELS', 'prism_shift_enabled'), units=None,
                conv_fn=None, cast_fn=bool, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Slit width (eV)',),
                dest_path=('EELS', 'filter_slit_width'), units='EV',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=spect_path + ('Slit inserted',),
                dest_path=('EELS', 'filter_slit_inserted'), units=None,
                conv_fn=None, cast_fn=bool, override=False),
        ]
        map_dict_values(mapping)

    def _dm3_eds_info(self) -> None:
        pre_path = self.__get_dm3_tag_pre_path()
        base = pre_path + ('EDS',)
        mapping = [
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Azimuthal angle'),
                dest_path=('EDS', 'azimuth_angle'), units='DEG',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Detector type'),
                dest_path=('EDS', 'detector_type'), units=None,
                conv_fn=None, cast_fn=str, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Acquisition', 'Dispersion (eV)'),
                dest_path=('EDS', 'dispersion_per_channel'), units='EV',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Elevation angle'),
                dest_path=('EDS', 'elevation_angle'), units='DEG',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Incidence angle'),
                dest_path=('EDS', 'incidence_angle'), units='DEG',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Live time',),
                dest_path=('EDS', 'live_time'), units='SEC',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Real time',),
                dest_path=('EDS', 'real_time'), units='SEC',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Solid angle'),
                dest_path=('EDS', 'solid_angle'), units='SR',
                conv_fn=None, cast_fn=float, override=False),
            MappingElements(
                source_dict=self.raw_meta, dest_dict=self.em,
                source_path=base + ('Detector Info', 'Stage tilt'),
                dest_path=('EDS', 'stage_tilt'), units='DEG',
                conv_fn=None, cast_fn=float, override=False)
        ]

        map_dict_values(mapping)

    def _dm3_spectrum_image_info(self) -> None:
        pass

    def _dm3_tecnai_info(self, delimiter: Optional[str] = u'\u2028') -> None:
        """
        Some FEI Microscopes will write additional metadata into dm3 files
        in a long string separated by a unicode delimiter (u'\u2028'),
        present at ``ImageList.TagGroup0.ImageTags.Tecnai.Microscope_Info``.
        This method parses that information.  Adapted from the
        implementation in https://github.com/usnistgov/NexusLIMS

        Parameters
        ----------
        delimiter
            The value (a unicode string) used to split the ``microscope_info``
            string. Should not need to be provided (this value is hard-coded in
            DigitalMicrograph), but specified as a parameter for future
            flexibility
        """
        def __find_val(s_to_find, list_to_search):
            """
            Return the first value in ``list_to_search`` that contains
            ``s_to_find``, or ``None`` if it is not found

            Note: If needed, this could be improved to use regex instead,
            which would provide more control over the patterns to return
            """
            res = [x for x in list_to_search if s_to_find in x]
            if len(res) > 0:
                res = res[0]
                # remove the string we searched for from the beginning of
                # the res
                return re.sub("^" + s_to_find, "", res)
            else:
                return None

        def __extract_val(regex: str, str_to_search: str,
                          match_num : int = 1) -> Optional[str]:
            """
            Extract a value from a string based on a grouped regex
            """
            result = re.compile(regex).search(str_to_search)
            if result is not None:
                result = result[match_num]
            return result

        path_to_tecnai = ('ImageList', 'TagGroup0', 'ImageTags', 'Tecnai',
                          'Microscope Info')
        self.tecnai_info = get_val(self.raw_meta, path_to_tecnai)

        if self.tecnai_info is None:
            # if tecnai info is not present, return early to save some work
            return
        else:
            # split the tecnai_info string into a list
            self.tecnai_info = self.tecnai_info.split(delimiter)

            # we override existing values since Tecnai info is more specific
            mapping = [
                MappingElements(
                    source_dict={
                        'Microscope_Name': __find_val('Microscope ',
                                                      self.tecnai_info)},
                    source_path='Microscope_Name', dest_dict=self.em,
                    dest_path=('General_EM', 'microscope_name'), cast_fn=str,
                    units=None, conv_fn=None, override=True),
                MappingElements(
                    source_dict={
                        'Extractor_Voltage':
                            __extract_val(r'Extr volt (\d*) V',
                                          __find_val('Extr volt ',
                                                     self.tecnai_info))},
                    source_path='Extractor_Voltage', dest_dict=self.em,
                    dest_path=('TEM', 'extractor_voltage'), cast_fn=int,
                    units='V', conv_fn=None, override=True),
                MappingElements(
                    source_dict={
                        'Emission_Current':
                            __extract_val(r'Emission ([\d|\.]*)uA',
                                          __find_val('Emission ',
                                                     self.tecnai_info))},
                    source_path='Emission_Current', dest_dict=self.em,
                    dest_path=('General_EM', 'emission_current'), cast_fn=float,
                    units='MicroA', conv_fn=None, override=True),
                MappingElements(
                    source_dict={
                        'Operation_Mode':
                            __extract_val(r'(.*) Defocus',
                                          __find_val('Mode ',
                                                     self.tecnai_info))},
                    source_path='Operation_Mode', dest_dict=self.em,
                    dest_path=('TEM', 'operation_mode'), cast_fn=str,
                    units=None, conv_fn=None, override=True),

                # try two different extractions of defocus for mag mode and
                # diffraction mode:
                MappingElements(
                    source_dict={
                        'Defocus':
                            __extract_val(r'Defocus \(um\) (.*) Magn',
                                          __find_val('Mode ',
                                                     self.tecnai_info))},
                    source_path='Defocus', dest_dict=self.em,
                    dest_path=('TEM', 'defocus'), cast_fn=float,
                    units='MicroM', conv_fn=None, override=True),
                MappingElements(
                    source_dict={
                        'Defocus':
                            __extract_val(r'Defocus ([\d|\.]*) CL',
                                          __find_val('Mode ',
                                                     self.tecnai_info))},
                    source_path='Defocus', dest_dict=self.em,
                    dest_path=('TEM', 'defocus'), cast_fn=float,
                    units='MicroM', conv_fn=None, override=True),
                # try magnification (not always present):
                MappingElements(
                    source_dict={
                        'Magnification':
                            __extract_val(r'Magn (\d*)x',
                                          __find_val('Mode ',
                                                     self.tecnai_info))},
                    source_path='Magnification', dest_dict=self.em,
                    dest_path=('General_EM', 'magnification_indicated'),
                    cast_fn=int, units='UNITLESS', conv_fn=None,
                    override=True),
                MappingElements(
                    source_dict={
                        'Camera_Length':
                            __extract_val(r'CL (.*)m',
                                          __find_val('Mode ',
                                                     self.tecnai_info))},
                    source_path='Camera_Length', dest_dict=self.em,
                    dest_path=('TEM', 'camera_length'), cast_fn=float,
                    units='MilliM', conv_fn=lambda x: x*1000, override=True),
                # spot size
                MappingElements(
                    source_dict={'Spot_Size':
                                     __find_val('Spot ', self.tecnai_info)},
                    source_path='Spot_Size', dest_dict=self.em,
                    dest_path=('TEM', 'spot_size'), cast_fn=int,
                    units='UNITLESS', conv_fn=None, override=True),
                # Tecnai has info about apertures and lens strengths,
                # but not extracting here (see NexusLIMS code for example)
            ]
            stage_vals = __find_val('Stage', self.tecnai_info)
            if stage_vals:
                x, y, z = re.findall(r' (-?\d*\.\d*) um', stage_vals)
                alpha, beta = re.findall(r' (-?\d*\.\d*) deg', stage_vals)
                stage = {'x': x, 'y': y, 'z': z, 'a': alpha, 'b': beta}
                mapping += [
                    MappingElements(
                        source_dict=stage, source_path='x', dest_dict=self.em,
                        dest_path=('General_EM', 'stage_position', 'x'),
                        cast_fn=float, units='MicroM', conv_fn=None,
                        override=True),
                    MappingElements(
                        source_dict=stage, source_path='y', dest_dict=self.em,
                        dest_path=('General_EM', 'stage_position', 'y'),
                        cast_fn=float, units='MicroM', conv_fn=None,
                        override=True),
                    MappingElements(
                        source_dict=stage, source_path='z', dest_dict=self.em,
                        dest_path=('General_EM', 'stage_position', 'z'),
                        cast_fn=float, units='MicroM', conv_fn=None,
                        override=True),
                    MappingElements(
                        source_dict=stage, source_path='a', dest_dict=self.em,
                        dest_path=('General_EM', 'stage_position',
                                   'tilt_alpha'), cast_fn=float, units='DEG',
                        conv_fn=None, override=True),
                    MappingElements(
                        source_dict=stage, source_path='b', dest_dict=self.em,
                        dest_path=('General_EM', 'stage_position', 'tilt_beta'),
                        cast_fn=float, units='DEG', conv_fn=None,
                        override=True),
                ]

            # process EELS spectrometer info from Tecnai string
            if __find_val('Filter related settings', self.tecnai_info):
                filter_dict = {
                    'Mode': __find_val('Mode: ', self.tecnai_info),
                    'Dispersion': __extract_val(
                        r'(.*)\[eV/Channel\]',
                        __find_val('Selected dispersion: ', self.tecnai_info)),
                    'Aperture': __extract_val(
                        r'(\d*)mm', __find_val('Selected aperture: ',
                                               self.tecnai_info)),
                    'Prism': __extract_val(
                        r'(.*)\[eV\]',
                        __find_val('Prism shift: ', self.tecnai_info)),
                    'Drift': __extract_val(
                        r'(.*)\[eV\]',
                        __find_val('Drift tube: ', self.tecnai_info)),
                    'TotalLoss': __extract_val(
                        r'(.*)\[eV\]',
                        __find_val('Total energy loss: ', self.tecnai_info))
                }
                mapping += [
                    MappingElements(
                        source_dict=filter_dict, source_path='Mode',
                        dest_dict=self.em,
                        dest_path=('EELS', 'spectrometer_mode'),
                        cast_fn=str, units=None, conv_fn=None, override=True),
                    MappingElements(
                        source_dict=filter_dict, source_path='Dispersion',
                        dest_dict=self.em,
                        dest_path=('EELS', 'dispersion_per_channel'),
                        cast_fn=float, units='EV', conv_fn=None, override=True),
                    MappingElements(
                        source_dict=filter_dict, source_path='Aperture',
                        dest_dict=self.em,
                        dest_path=('EELS', 'aperture_size'),
                        cast_fn=float, units='MilliM', conv_fn=None,
                        override=True),
                    MappingElements(
                        source_dict=filter_dict, source_path='Drift',
                        dest_dict=self.em, dest_path=('EELS',
                                                      'drift_tube_energy'),
                        cast_fn=float, units='EV', conv_fn=None, override=True),
                    MappingElements(
                        source_dict=filter_dict, source_path='Prism',
                        dest_dict=self.em, dest_path=('EELS',
                                                      'prism_shift_energy'),
                        cast_fn=float, units='EV', conv_fn=None, override=True),
                    MappingElements(
                        source_dict=filter_dict, source_path='TotalLoss',
                        dest_dict=self.em, dest_path=('EELS',
                                                      'total_energy_loss'),
                        cast_fn=float, units='EV', conv_fn=None, override=True),
                ]

            map_dict_values(mapping)

    def _tia_info(self) -> None:
        pass

    def _tiff_info(self) -> None:
        pass

    def implementors(self):
        return ['Jonathon Gaff <jgaff@uchicago.edu>',
                'Joshua Taillon <joshua.taillon@nist.gov>']

    def version(self):
        return '0.1.0'
