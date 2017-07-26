# See COPYRIGHT file at the top of the source tree.

from __future__ import division, absolute_import, print_function

import sys
import traceback

import numpy as np

import lsst.utils
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.pex.exceptions as pexExceptions
import lsst.afw.table as afwTable
from lsst.daf.base.dateTime import DateTime
import lsst.afw.geom as afwGeom
import lsst.daf.persistence.butlerExceptions as butlerExceptions

from detectorThroughput import DetectorThroughput

import time

import fgcm

__all__ = ['FgcmMakeLutConfig', 'FgcmMakeLutTask']

class FgcmMakeLutConfig(pexConfig.Config):
    """Config for FgcmMakeLutTask"""

    bands = pexConfig.ListField(
        doc="Bands to build LUT",
        dtype=str,
        default=("NO_DATA",),
        )
    elevation = pexConfig.Field(
        doc="Telescope elevation (m)",
        dtype=float,
        default=None,
        )
    pmbRange = pexConfig.ListField(
        doc="Barometric Pressure range (millibar)",
        dtype=float,
        default=(770.0,790.0,),
        )
    pmbSteps = pexConfig.Field(
        doc="Barometric Pressure number of steps",
        dtype=int,
        default=5,
        )
    pwvRange = pexConfig.ListField(
        doc="Precipitable Water Vapor range (mm)",
        dtype=float,
        default=(0.1,12.0,),
        )
    pwvSteps = pexConfig.Field(
        doc="Precipitable Water Vapor number of steps",
        dtype=int,
        default=21,
        )
    o3Range = pexConfig.ListField(
        doc="Ozone range (dob)",
        dtype=float,
        default=(220.0,310.0,),
        )
    o3Steps = pexConfig.Field(
        doc="Ozone number of steps",
        dtype=int,
        default=3,
        )
    tauRange = pexConfig.ListField(
        doc="Aerosol Optical Depth range (unitless)",
        dtype=float,
        default=(0.002,0.25,),
        )
    tauSteps = pexConfig.Field(
        doc="Aerosol Optical Depth number of steps",
        dtype=int,
        default=11,
        )
    alphaRange = pexConfig.ListField(
        doc="Aerosol alpha range (unitless)",
        dtype=float,
        default=(0.0,2.0),
        )
    alphaSteps = pexConfig.Field(
        doc="Aerosol alpha number of steps",
        dtype=int,
        default=9,
        )
    zenithRange = pexConfig.ListField(
        doc="Zenith angle range (degree)",
        dtype=float,
        default=(0.0,70.0,),
        )
    zenithSteps = pexConfig.Field(
        doc="Zenith angle number of steps",
        dtype=int,
        default=21,
        )
    pmbStd = pexConfig.Field(
        doc="Standard Atmosphere pressure (millibar)",
        dtype=float,
        default=None,
        )
    pwvStd = pexConfig.Field(
        doc="Standard Atmosphere PWV (mm)",
        dtype=float,
        default=None,
        )
    o3Std = pexConfig.Field(
        doc="Standard Atmosphere O3 (dob)",
        dtype=float,
        default=None,
        )
    tauStd = pexConfig.Field(
        doc="Standard Atmosphere aerosol optical depth",
        dtype=float,
        default=None,
        )
    alphaStd = pexConfig.Field(
        doc="Standard Atmosphere aerosol alpha",
        dtype=float,
        default=None,
        )
    airmassStd = pexConfig.Field(
        doc="Standard Atmosphere airmass",
        dtype=float,
        default=None,
        )
    lambdaNorm = pexConfig.Field(
        doc="Aerosol Optical Depth normalization wavelength (A)",
        dtype=float,
        default=None,
        )
    lambdaStep = pexConfig.Field(
        doc="Wavelength step for generating atmospheres (nm)",
        dtype=float,
        default=0.5,
        )
    lambdaRange = pexConfig.ListField(
        doc="Wavelength range for LUT (A)",
        dtype=float,
        default=None,
        )

    def setDefaults(self):
        pass

class FgcmMakeLutRunner(pipeBase.ButlerInitializedTaskRunner):
    """Subclass of TaskRunner for fgcmMakeLutTask

    """

    @staticmethod
    def getTargetList(parsedCmd):
        return [parsedCmd.butler]

    def precall(self, parsedCmd):
        return True

    def __call__(self, butler):
        task = self.TaskClass(config=self.config, log=self.log)
        if self.doRaise:
            results = task.run(butler)
        else:
            try:
                results = task.run(butler)
            except Exception as e:
                task.log.fatal("Failed: %s" % e)
                if not isinstance(e, pipeBase.TaskError):
                    traceback.print_exc(file=sys.stderr)

        task.writeMetadata(butler)
        if self.doReturnResults:
            return results

    # turn off any multiprocessing

    def run(self, parsedCmd):
        """ runs the task, but doesn't do multiprocessing"""

        resultList = []

        if self.precall(parsedCmd):
            profileName = parsedCmd.profile if hasattr(parsedCmd, "profile") else None
            log = parsedCmd.log
            targetList = self.getTargetList(parsedCmd)
            # make sure that we only get 1
            resultList = self(targetList[0])

        return resultList

class FgcmMakeLutTask(pipeBase.CmdLineTask):
    """
    Make Look-Up Table for FGCM global calibration
    """

    ConfigClass = FgcmMakeLutConfig
    RunnerClass = FgcmMakeLutRunner
    _DefaultName = "fgcmMakeLut"

    def __init__(self, butler=None, **kwargs):
        """
        Instantiate an fgcmMakeLutTask.

        Parameters
        ----------
        butler : lsst.daf.persistence.Butler
          Something about the butler
        """

        pipeBase.CmdLineTask.__init__(self, **kwargs)

    @classmethod
    def _makeArgumentParser(cls):
        """Create an argument parser"""

        parser = pipeBase.ArgumentParser(name=cls._DefaultName)

        return parser

    # no saving of metadata for now
    def _getMetadataName(self):
        return None

    @pipeBase.timeMethod
    def run(self, butler):
        """
        Make a Look-Up Table for FGCM

        Parameters
        ----------
        butler:  a butler.

        Returns
        -------
        nothing?
        """

        if (not butler.datasetExists('fgcmLut')):
            self._fgcmMakeLut(butler)

        return None

    def _fgcmMakeLut(self, butler):
        """
        """

        if (butler.datasetExists('fgcmLookUpTable')):
            # all done
            return

        # need the camera for the detectors
        camera = butler.get('camera')

        # number of ccds from the length of the camera iterator
        nCcd = len(camera)

        # make the config dictionary
        lutConfig = {}
        lutConfig['elevation'] = self.config.elevation
        lutConfig['bands'] = self.config.bands
        lutConfig['nCCD'] = nCcd
        lutConfig['pmbRange'] = self.config.pmbRange
        lutConfig['pmbSteps'] = self.config.pmbSteps
        lutConfig['pwvRange'] = self.config.pwvRange
        lutConfig['pwvSteps'] = self.config.pwvSteps
        lutConfig['o3Range'] = self.config.o3Range
        lutConfig['o3Steps'] = self.config.o3Steps
        lutConfig['tauRange'] = self.config.tauRange
        lutConfig['tauSteps'] = self.config.tauSteps
        lutConfig['alphaRange'] = self.config.alphaRange
        lutConfig['alphaSteps'] = self.config.alphaSteps
        lutConfig['zenithRange'] = self.config.zenithRange
        lutConfig['zenithSteps'] = self.config.zenithSteps
        lutConfig['pmbStd'] = self.config.pmbStd
        lutConfig['pwvStd'] = self.config.pwvStd
        lutConfig['o3Std'] = self.config.p3Std
        lutConfig['tauStd'] = self.config.tauStd
        lutConfig['alphaStd'] = self.config.alphaStd
        lutConfig['airmassStd'] = self.config.airmassStd
        lutConfig['lambdaRange'] = self.config.lambdaRange
        lutConfig['lambdaStep'] = self.config.lambdaStep
        lutConfig['lambdaNorm'] = self.config.lambdaNorm

        # make the lut object
        self.fgcmLutMaker = fgcm.FgcmLUTMaker(lutConfig)

        # generate the throughput dictionary.  Fun!
        # do this internally here at first.  Later, break it into its own thing

        # these will be in Angstroms
        # note that lambdaStep is currently in nm, because dumb.  convert to A
        throughLambda = np.arange(self.config.lambdaRange[0],
                                  self.config.lambdaRange[1],
                                  self.config.lambdaStep*10.)

        tput = DetectorThroughput()

        throughputDict = {}
        for i,b in enumerate(self.config.bands):
            tDict = {}
            tDict['LAMBDA'] = throughputLambda
            for ccdIndex,detector in enumerate(camera):
                # make sure we convert the calling units from A to nm
                tDict[ccdIndex] = tput.getThroughputDetector(detector, band,
                                                             throughputLambda/10.)
            throughputDict[b] = tDict

        # set the throughputs
        self.fgcmLutMaker.setThroughputs(throughputDict)

        # make the LUT
        self.fgcmLutMaker.makeLUT()

        # and save the LUT
        lutSchema = afwTable.Schema()

        # build the index values
        comma=','
        bandString = comma.join(self.config.bands)
        lutSchema.addField('bands', type=str, doc='Bands in LUT', size=len(bandString))
        lutSchema.addField('pmb', type=np.float64, doc='Barometric Pressure',
                           unit='millibar', size=self.fgcmLutMaker.pmb.size)
        lutSchema.addField('pmbfactor', type=np.float64, doc='PMB scaling factor',
                           size=self.fgcmLutMaker.pmb.size)
        lutSchema.addField('pmbElevation', type=np.float64, doc='PMB Scaling at elevation')
        lutSchema.addField('pwv', type=np.float64, doc='Preciptable Water Vapor',
                           unit='mm', size=self.fgcmLutMaker.pwv.size)
        lutSchema.addField('o3', type=np.float64, doc='Ozone',
                           unit='dobson', size=self.fgcmLutMaker.o3.size)
        lutSchema.addField('tau', type=np.float64, doc='Aerosol optical depth',
                           size=self.fgcmLutMaker.tau.size)
        lutSchema.addField('lambdanorm', type=np.float64, doc='AOD wavelength',
                           unit='Angstrom')
        lutSchema.addField('alpha', type=np.float64, doc='Aerosol alpha',
                           size=self.fgcmLutMaker.alpha.size)
        lutSchema.addField('zenith', type=np.float64, doc='Zenith angle',
                           unit='degrees', size=self.fgcmLutMaker.zenith.size)
        lutSchema.addField('nccd', type=np.int32, doc='Number of CCDs')

        # and the standard values
        lutSchema.addField('pmbstd', type=np.float64, doc='PMB Standard',
                           unit='millibar')
        lutSchema.addField('pwvstd', type=np.float64, doc='PWV Standard',
                           unit='mm')
        lutSchema.addField('o3std', type=np.float64, doc='O3 Standard',
                           unit='dobson')
        lutSchema.addField('taustd', type=np.float64, doc='Tau Standard')
        lutSchema.addField('alphastd', type=np.float64, doc='Alpha Standard')
        lutSchema.addField('zenithstd', type=np.float64, doc='Zenith angle Standard',
                           unit='degree')
        lutSchema.addField('lambdarange', type=np.float64, doc='Wavelength range',
                           unit='A', size=2)
        lutSchema.addField('lambdastep', type=np.float64, doc='Wavelength step',
                           unit='nm')
        lutSchema.addField('lambdastd', type=np.float64, doc='Standard Wavelength',
                           unit='A', size=self.fgcmLutMaker.bands.size)
        lutSchema.addField('i0std', type=np.float64, doc='I0 Standard',
                           size=self.fgcmLutMaker.bands.size)
        lutSchema.addField('i1std', type=np.float64, doc='I1 Standard',
                           size=self.fgcmLutMaker.bands.size)
        lutSchema.addField('i10std', type=np.float64, doc='I10 Standard',
                           size=self.fgcmLutMaker.bands.size)
        lutSchema.addField('lambdab', type=np.float64, doc='Wavelength for passband (no atm)',
                           units='A', size=self.fgcmLutMaker.bands.size)
        lutSchema.addField('atmlambda', type=np.float64, doc='Atmosphere wavelengths',
                           units='A', size=self.fgcmLutMaker.atmLambda.size)
        lutSchema.addField('atmstdtrans', type=np.float64, doc='Standard Atmosphere Throughput',
                           size=self.fgcmLutMaker.atmStd.size)

        # and the LUT
        lutSchema.addField('lut', type=np.float32, doc='Look-up table',
                           size=self.fgcmLutMaker.lut.size)

        # and the LUT derivatives
        lutSchema.addField('lutderiv', type=np.float32, doc='Derivative look-up table',
                           size=self.fgcmLutMaker.lutDeriv.size)

        lutCat = afwTable.BaseCatalog(lutSchema)
        lutCat.table.preallocate(1)
        rec = lutCat.addNew()

        rec['bands'] = bandString
        rec['pmb'][:] = self.fgcmLutMaker.pmb
        rec['pmbfactor'][:] = self.fgcmLutMaker.pmbFactor
        rec['pmbElevation'][:] = self.fgcmLutMaker.pmbElevation
        rec['pwv'][:] = self.fgcmLutMaker.pwv
        rec['o3'][:] = self.fgcmLutMaker.o3
        rec['tau'][:] = self.fgcmLutMaker.tau
        rec['lambdanorm'][:] = self.fgcmLutMaker.lambdaNorm
        rec['alpha'][:] = self.fgcmLutMaker.alpha
        rec['zenith'][:] = self.fgcmLutMaker.zenith
        rec['nccd'][:] = self.fgcmLutMaker.nCCD

        rec['pmbstd'][:] = self.fgcmLutMaker.pmbStd
        rec['pwvstd'][:] = self.fgcmLutMaker.pwvStd
        rec['o3std'][:] = self.fgcmLutMaker.o3Std
        rec['taustd'][:] = self.fgcmLutMaker.tauStd
        rec['alphastd'][:] = self.fgcmLutMaker.alphaStd
        rec['zenithstd'][:] = self.fgcmLutMaker.zenithStd
        rec['lambdarange'][:] = self.fgcmLutMaker.lambdaRange
        rec['lambdastep'][:] = self.fgcmLutMaker.lambdaStep
        rec['lambdastd'][:] = self.fgcmLutMaker.lambdaStd
        rec['i0std'][:] = self.fgcmLutMaker.i0Std
        rec['i1std'][:] = self.fgcmLutMaker.i1Std
        rec['i10std'][:] = self.fgcmLutMaker.i10Std
        rec['lambdab'][:] = self.fgcmLutMaker.lambdab
        rec['atmlambda'][:] = self.fgcmLutMaker.atmlambda
        rec['atmstdtrans'][:] = self.fgcmLutMaker.atmstdtrans

        rec['lut'][:] = self.fgcmLutMaker.lut.flatten()
        rec['lutderiv'][:] = self.fgcmLutMaker.lutDeriv.flatten()

        butler.put(lutCat, 'fgcmLookUpTable')

        # and we're done