# See COPYRIGHT file at the top of the source tree.
#
# This file is part of fgcmcal.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Run fgcmcal on a single tract.

"""

import sys
import traceback

import numpy as np

import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from lsst.jointcal.dataIds import PerTractCcdDataIdContainer

from .fgcmBuildStars import FgcmBuildStarsTask
from .fgcmFitCycle import FgcmFitCycleConfig
from .fgcmOutputProducts import FgcmOutputProductsTask
from .utilities import makeConfigDict, translateFgcmLut, translateVisitCatalog
from .utilities import computeCcdOffsets
from .utilities import makeZptSchema, makeZptCat
from .utilities import makeAtmSchema, makeAtmCat
from .utilities import makeStdSchema, makeStdCat

import fgcm

__all__ = ['FgcmCalibrateTractConfig', 'FgcmCalibrateTractTask', 'FgcmCalibrateTractRunner']


class FgcmCalibrateTractConfig(pexConfig.Config):
    """Config for FgcmCalibrateTract"""

    fgcmBuildStars = pexConfig.ConfigurableField(
        target=FgcmBuildStarsTask,
        doc="Task to load and match stars for fgcm",
    )
    fgcmFitCycle = pexConfig.ConfigField(
        dtype=FgcmFitCycleConfig,
        doc="Config to run a single fgcm fit cycle",
    )
    fgcmOutputProducts = pexConfig.ConfigurableField(
        target=FgcmOutputProductsTask,
        doc="Task to output fgcm products",
    )
    convergenceTolerance = pexConfig.Field(
        doc="Tolerance on repeatability convergence (per band)",
        dtype=float,
        default=0.005,
    )
    maxFitCycles = pexConfig.Field(
        doc="Maximum number of fit cycles",
        dtype=int,
        default=5,
    )
    doDebuggingPlots = pexConfig.Field(
        doc="Make plots for debugging purposes?",
        dtype=bool,
        default=False,
    )

    def setDefaults(self):
        pexConfig.Config.setDefaults(self)

        self.fgcmBuildStars.checkAllCcds = True
        self.fgcmFitCycle.useRepeatabilityForExpGrayCuts = True
        self.fgcmFitCycle.quietMode = True
        self.fgcmOutputProducts.doReferenceCalibration = False
        self.fgcmOutputProducts.doRefcatOutput = False
        self.fgcmOutputProducts.cycleNumber = 0
        self.fgcmOutputProducts.photoCal.applyColorTerms = False


class FgcmCalibrateTractRunner(pipeBase.ButlerInitializedTaskRunner):
    """Subclass of TaskRunner for FgcmCalibrateTractTask

    fgcmCalibrateTractTask.run() takes a number of arguments, one of which is
    the butler (for persistence and mapper data), and a list of dataRefs
    extracted from the command line.  This task runs on a constrained set
    of dataRefs, typically a single tract.
    This class transforms the process arguments generated by the ArgumentParser
    into the arguments expected by FgcmCalibrateTractTask.run().
    This runner does not use any parallelization.
    """

    @staticmethod
    def getTargetList(parsedCmd):
        """
        Return a list with one element: a tuple with the butler and
        list of dataRefs
        """
        # we want to combine the butler with any (or no!) dataRefs
        return [(parsedCmd.butler, parsedCmd.id.refList)]

    def __call__(self, args):
        """
        Parameters
        ----------
        args: `tuple` with (butler, dataRefList)

        Returns
        -------
        exitStatus: `list` with `pipeBase.Struct`
           exitStatus (0: success; 1: failure)
        """
        butler, dataRefList = args

        task = self.TaskClass(config=self.config, log=self.log)

        exitStatus = 0
        if self.doRaise:
            results = task.runDataRef(butler, dataRefList)
        else:
            try:
                results = task.runDataRef(butler, dataRefList)
            except Exception as e:
                exitStatus = 1
                task.log.fatal("Failed: %s" % e)
                if not isinstance(e, pipeBase.TaskError):
                    traceback.print_exc(file=sys.stderr)

        task.writeMetadata(butler)

        if self.doReturnResults:
            return [pipeBase.Struct(exitStatus=exitStatus,
                                    results=results)]
        else:
            return [pipeBase.Struct(exitStatus=exitStatus)]

    def run(self, parsedCmd):
        """
        Run the task, with no multiprocessing

        Parameters
        ----------
        parsedCmd: ArgumentParser parsed command line
        """

        resultList = []

        if self.precall(parsedCmd):
            targetList = self.getTargetList(parsedCmd)
            resultList = self(targetList[0])

        return resultList


class FgcmCalibrateTractTask(pipeBase.CmdLineTask):
    """
    Calibrate a single tract using fgcmcal
    """

    ConfigClass = FgcmCalibrateTractConfig
    RunnerClass = FgcmCalibrateTractRunner
    _DefaultName = "fgcmCalibrateTract"

    def __init__(self, butler=None, **kwargs):
        """
        Instantiate an FgcmCalibrateTractTask.

        Parameters
        ----------
        butler : `lsst.daf.persistence.Butler`
        """

        pipeBase.CmdLineTask.__init__(self, **kwargs)

    @classmethod
    def _makeArgumentParser(cls):
        """Create an argument parser"""

        parser = pipeBase.ArgumentParser(name=cls._DefaultName)
        parser.add_id_argument("--id", "calexp", help="Data ID, e.g. --id visit=6789",
                               ContainerClass=PerTractCcdDataIdContainer)

        return parser

    # no saving of metadata for now
    def _getMetadataName(self):
        return None

    @pipeBase.timeMethod
    def runDataRef(self, butler, dataRefs):
        """
        Run full FGCM calibration on a single tract, including building star list,
        fitting multiple cycles, and making outputs.

        Parameters
        ----------
        butler:  `lsst.daf.persistence.Butler`
        dataRefs: `list` of `lsst.daf.persistence.ButlerDataRef`
           Data references for the input visits.
           If this is an empty list, all visits with src catalogs in
           the repository are used.
           Only one individual dataRef from a visit need be specified
           and the code will find the other source catalogs from
           each visit.

        Raises
        ------
        RuntimeError: Raised if config.fgcmBuildStars.doReferenceMatches is
           not True, or if fgcmLookUpTable is not available.
        """

        if not butler.datasetExists('fgcmLookUpTable'):
            raise RuntimeError("Must run FgcmCalibrateTract with an fgcmLookUpTable")

        if not self.config.fgcmBuildStars.doReferenceMatches:
            raise RuntimeError("Must run FgcmCalibrateTract with fgcmBuildStars.doReferenceMatches")

        self.makeSubtask("fgcmBuildStars", butler=butler)
        self.makeSubtask("fgcmOutputProducts", butler=butler)

        # Run the build stars tasks
        tract = dataRefs[0].dataId['tract']
        self.log.info("Running on tract %d" % (tract))

        # Note that we will need visitCat at the end of the procedure for the outputs
        visitCat = self.fgcmBuildStars.fgcmMakeVisitCatalog(butler, dataRefs)
        fgcmStarObservationCat = self.fgcmBuildStars.fgcmMakeAllStarObservations(butler,
                                                                                 visitCat)
        fgcmStarIdCat, fgcmStarIndicesCat, fgcmRefCat = \
            self.fgcmBuildStars.fgcmMatchStars(butler,
                                               visitCat,
                                               fgcmStarObservationCat)

        # Load the LUT
        lutCat = butler.get('fgcmLookUpTable')
        fgcmLut, lutIndexVals, lutStd = translateFgcmLut(lutCat,
                                                         dict(self.config.fgcmFitCycle.filterMap))
        del lutCat

        # Translate the visit catalog into fgcm format
        fgcmExpInfo = translateVisitCatalog(visitCat)

        camera = butler.get('camera')
        configDict = makeConfigDict(self.config.fgcmFitCycle, self.log, camera,
                                    self.config.fgcmFitCycle.maxIterBeforeFinalCycle,
                                    True, False, tract=tract)
        # Turn off plotting in tract mode
        configDict['doPlots'] = False
        ccdOffsets = computeCcdOffsets(camera, self.config.fgcmFitCycle.pixelScale)
        del camera

        # Set up the fit cycle task

        noFitsDict = {'lutIndex': lutIndexVals,
                      'lutStd': lutStd,
                      'expInfo': fgcmExpInfo,
                      'ccdOffsets': ccdOffsets}

        fgcmFitCycle = fgcm.FgcmFitCycle(configDict, useFits=False,
                                         noFitsDict=noFitsDict, noOutput=True)

        # We determine the conversion from the native units (typically radians) to
        # degrees for the first star.  This allows us to treat coord_ra/coord_dec as
        # numpy arrays rather than Angles, which would we approximately 600x slower.
        conv = fgcmStarObservationCat[0]['ra'].asDegrees() / float(fgcmStarObservationCat[0]['ra'])

        # To load the stars, we need an initial parameter object
        fgcmPars = fgcm.FgcmParameters.newParsWithArrays(fgcmFitCycle.fgcmConfig,
                                                         fgcmLut,
                                                         fgcmExpInfo)

        # Match star observations to visits
        # Only those star observations that match visits from fgcmExpInfo['VISIT'] will
        # actually be transferred into fgcm using the indexing below.

        obsIndex = fgcmStarIndicesCat['obsIndex']
        visitIndex = np.searchsorted(fgcmExpInfo['VISIT'],
                                     fgcmStarObservationCat['visit'][obsIndex])

        fgcmStars = fgcm.FgcmStars(fgcmFitCycle.fgcmConfig)
        fgcmStars.loadStars(fgcmPars,
                            fgcmStarObservationCat['visit'][obsIndex],
                            fgcmStarObservationCat['ccd'][obsIndex],
                            fgcmStarObservationCat['ra'][obsIndex] * conv,
                            fgcmStarObservationCat['dec'][obsIndex] * conv,
                            fgcmStarObservationCat['instMag'][obsIndex],
                            fgcmStarObservationCat['instMagErr'][obsIndex],
                            fgcmExpInfo['FILTERNAME'][visitIndex],
                            fgcmStarIdCat['fgcm_id'][:],
                            fgcmStarIdCat['ra'][:],
                            fgcmStarIdCat['dec'][:],
                            fgcmStarIdCat['obsArrIndex'][:],
                            fgcmStarIdCat['nObs'][:],
                            obsX=fgcmStarObservationCat['x'][obsIndex],
                            obsY=fgcmStarObservationCat['y'][obsIndex],
                            refID=fgcmRefCat['fgcm_id'][:],
                            refMag=fgcmRefCat['refMag'][:, :],
                            refMagErr=fgcmRefCat['refMagErr'][:, :],
                            flagID=None,
                            flagFlag=None,
                            computeNobs=True)

        fgcmFitCycle.setLUT(fgcmLut)
        fgcmFitCycle.setStars(fgcmStars)

        # Clear out some memory
        # del fgcmStarObservationCat
        del fgcmStarIdCat
        del fgcmStarIndicesCat
        del fgcmRefCat

        converged = False
        cycleNumber = 0

        previousReservedRawRepeatability = np.zeros(fgcmPars.nBands) + 1000.0
        previousParInfo = None
        previousParams = None
        previousSuperStar = None

        while (not converged and cycleNumber < self.config.maxFitCycles):

            fgcmFitCycle.fgcmConfig.updateCycleNumber(cycleNumber)

            if cycleNumber > 0:
                # Use parameters from previous cycle
                fgcmPars = fgcm.FgcmParameters.loadParsWithArrays(fgcmFitCycle.fgcmConfig,
                                                                  fgcmExpInfo,
                                                                  previousParInfo,
                                                                  previousParams,
                                                                  previousSuperStar)
                # We need to reset the star magnitudes and errors for the next
                # cycle
                fgcmFitCycle.fgcmStars.reloadStarMagnitudes(fgcmStarObservationCat['instMag'][obsIndex],
                                                            fgcmStarObservationCat['instMagErr'][obsIndex])
                fgcmFitCycle.initialCycle = False

            fgcmFitCycle.setPars(fgcmPars)
            fgcmFitCycle.finishSetup()

            fgcmFitCycle.run()

            # Grab the parameters for the next cycle
            previousParInfo, previousParams = fgcmFitCycle.fgcmPars.parsToArrays()
            previousSuperStar = fgcmFitCycle.fgcmPars.parSuperStarFlat.copy()

            self.log.info("Raw repeatability after cycle number %d is:" % (cycleNumber))
            for i, band in enumerate(fgcmFitCycle.fgcmPars.bands):
                rep = fgcmFitCycle.fgcmPars.compReservedRawRepeatability[i] * 1000.0
                self.log.info("  Band %s, repeatability: %.2f mmag" % (band, rep))

            # Check for convergence
            if np.alltrue((previousReservedRawRepeatability -
                           fgcmFitCycle.fgcmPars.compReservedRawRepeatability) <
                          self.config.convergenceTolerance):
                self.log.info("Raw repeatability has converged after cycle number %d." % (cycleNumber))
                converged = True
            else:
                fgcmFitCycle.fgcmConfig.expGrayPhotometricCut[:] = fgcmFitCycle.updatedPhotometricCut
                fgcmFitCycle.fgcmConfig.expGrayHighCut[:] = fgcmFitCycle.updatedHighCut
                fgcmFitCycle.fgcmConfig.precomputeSuperStarInitialCycle = False
                fgcmFitCycle.fgcmConfig.freezeStdAtmosphere = False
                previousReservedRawRepeatability[:] = fgcmFitCycle.fgcmPars.compReservedRawRepeatability
                self.log.info("Setting exposure gray photometricity cuts to:")
                for i, band in enumerate(fgcmFitCycle.fgcmPars.bands):
                    cut = fgcmFitCycle.updatedPhotometricCut[i] * 1000.0
                    self.log.info("  Band %s, photometricity cut: %.2f mmag" % (band, cut))

            cycleNumber += 1

        # Log warning if not converged
        if not converged:
            self.log.warn("Maximum number of fit cycles exceeded (%d) without convergence." % (cycleNumber))

        # Do final clean-up iteration
        fgcmFitCycle.fgcmConfig.freezeStdAtmosphere = False
        fgcmFitCycle.fgcmConfig.resetParameters = False
        fgcmFitCycle.fgcmConfig.maxIter = 0
        fgcmFitCycle.fgcmConfig.outputZeropoints = True
        fgcmFitCycle.fgcmConfig.outputStandards = True
        fgcmFitCycle.fgcmConfig.doPlots = self.config.doDebuggingPlots
        fgcmFitCycle.fgcmConfig.updateCycleNumber(cycleNumber)
        fgcmFitCycle.initialCycle = False

        fgcmPars = fgcm.FgcmParameters.loadParsWithArrays(fgcmFitCycle.fgcmConfig,
                                                          fgcmExpInfo,
                                                          previousParInfo,
                                                          previousParams,
                                                          previousSuperStar)
        fgcmFitCycle.fgcmStars.reloadStarMagnitudes(fgcmStarObservationCat['instMag'][obsIndex],
                                                    fgcmStarObservationCat['instMagErr'][obsIndex])
        fgcmFitCycle.setPars(fgcmPars)
        fgcmFitCycle.finishSetup()

        self.log.info("Running final clean-up fit cycle...")
        fgcmFitCycle.run()

        self.log.info("Raw repeatability after clean-up cycle is:")
        for i, band in enumerate(fgcmFitCycle.fgcmPars.bands):
            rep = fgcmFitCycle.fgcmPars.compReservedRawRepeatability[i] * 1000.0
            self.log.info("  Band %s, repeatability: %.2f mmag" % (band, rep))

        # Do the outputs.  Need to keep track of tract, blah.

        if self.config.fgcmFitCycle.superStarSubCcd or self.config.fgcmFitCycle.ccdGraySubCcd:
            chebSize = fgcmFitCycle.fgcmZpts.zpStruct['FGCM_FZPT_CHEB'].shape[1]
        else:
            chebSize = 0

        zptSchema = makeZptSchema(chebSize)
        zptCat = makeZptCat(zptSchema, fgcmFitCycle.fgcmZpts.zpStruct)

        atmSchema = makeAtmSchema()
        atmCat = makeAtmCat(atmSchema, fgcmFitCycle.fgcmZpts.atmStruct)

        stdSchema = makeStdSchema(fgcmFitCycle.fgcmPars.nBands)
        stdStruct = fgcmFitCycle.fgcmStars.retrieveStdStarCatalog(fgcmFitCycle.fgcmPars)
        stdCat = makeStdCat(stdSchema, stdStruct)

        outStruct = self.fgcmOutputProducts.generateOutputProducts(butler, tract,
                                                                   visitCat,
                                                                   zptCat, atmCat, stdCat,
                                                                   self.config.fgcmBuildStars,
                                                                   self.config.fgcmFitCycle)
        outStruct.repeatability = fgcmFitCycle.fgcmPars.compReservedRawRepeatability

        return outStruct
