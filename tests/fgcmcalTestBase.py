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
"""General fgcmcal testing class.

This class is used as the basis for individual obs package tests using
data from testdata_jointcal for Gen3 repos.
"""

import os
import shutil
import numpy as np
import glob
import esutil

import click.testing
import lsst.ctrl.mpexec.cli.pipetask

import lsst.daf.butler as dafButler
import lsst.obs.base as obsBase
import lsst.geom as geom
import lsst.log

import lsst.fgcmcal as fgcmcal

ROOT = os.path.abspath(os.path.dirname(__file__))


class FgcmcalTestBase(object):
    """Base class for gen3 fgcmcal tests, to genericize some test running and setup.

    Derive from this first, then from TestCase.
    """
    def setUp_base(self, testDir):
        """Common routines to set up tests.

        Parameters
        ----------
        testDir : `str`
            Temporary directory to run tests in.
        """
        self.testDir = testDir

    def _importRepository(self, instrument, exportPath, exportFile):
        """Import a test repository into self.testDir

        Parameters
        ----------
        instrument : `str`
            Full string name for the instrument.
        exportPath : `str`
            Path to location of repository to export.
        exportFile : `str`
            Filename of export data.
        """
        self.repo = os.path.join(self.testDir, 'testrepo')

        print('Importing %s into %s' % (exportFile, self.testDir))

        # Make the repo and retrieve a writeable Butler
        _ = dafButler.Butler.makeRepo(self.repo)
        butler = dafButler.Butler(self.repo, writeable=True)
        # Register the instrument
        instrInstance = obsBase.utils.getInstrument(instrument)
        instrInstance.register(butler.registry)
        # Import the exportFile
        butler.import_(directory=exportPath, filename=exportFile,
                       transfer='symlink',
                       skip_dimensions={'instrument', 'detector', 'physical_filter'})

    def _runPipeline(self, repo, pipelineFile, queryString=None,
                     inputCollections=None, outputCollection=None,
                     configFiles=None, configOptions=None,
                     registerDatasetTypes=False):
        """Run a pipeline via pipetask.

        Parameters
        ----------
        repo : `str`
            Gen3 repository yaml file.
        pipelineFile : `str`
            Pipeline definition file.
        queryString : `str`, optional
            String to use for "-d" data query.
        inputCollections : `str`, optional
            String to use for "-i" input collections (comma delimited).
        outputCollection : `str`, optional
            String to use for "-o" output collection.
        configFiles : `list` [`str`], optional
            List of config files to use (with "-C").
        configOptions : `list` [`str`], optional
            List of individual config options to use (with "-c").
        registerDatasetTypes : `bool`, optional
            Set "--register-dataset-types".

        Returns
        -------
        exit_code : `int`
            Exit code for pipetask run.

        Raises
        ------
        RuntimeError : Raised if the "pipetask" call fails.
        """
        pipelineArgs = ["run",
                        "-b", repo,
                        "-p", pipelineFile]

        if queryString is not None:
            pipelineArgs.extend(["-d", queryString])
        if inputCollections is not None:
            pipelineArgs.extend(["-i", inputCollections])
        if outputCollection is not None:
            pipelineArgs.extend(["-o", outputCollection])
        if configFiles is not None:
            for configFile in configFiles:
                pipelineArgs.extend(["-C", configFile])
        if configOptions is not None:
            for configOption in configOptions:
                pipelineArgs.extend(["-c", configOption])
        if registerDatasetTypes:
            pipelineArgs.extend(["--register-dataset-types"])

        # CliRunner is an unsafe workaround for DM-26239
        runner = click.testing.CliRunner()
        results = runner.invoke(lsst.ctrl.mpexec.cli.pipetask.cli, pipelineArgs)
        if results.exception:
            raise RuntimeError("Pipeline %s failed." % (pipelineFile)) from results.exception
        return results.exit_code

    def _testFgcmMakeLut(self, instName, nBand, i0Std, i0Recon, i10Std, i10Recon):
        """Test running of FgcmMakeLutTask

        Parameters
        ----------
        instName : `str`
            Short name of the instrument
        nBand : `int`
            Number of bands tested
        i0Std : `np.ndarray'
            Values of i0Std to compare to
        i10Std : `np.ndarray`
            Values of i10Std to compare to
        i0Recon : `np.ndarray`
            Values of reconstructed i0 to compare to
        i10Recon : `np.ndarray`
            Values of reconsntructed i10 to compare to
        """
        instCamel = instName.title()

        configFile = 'fgcmMakeLut:' + os.path.join(ROOT,
                                                   'config',
                                                   'fgcmMakeLut%s.py' % (instCamel))
        outputCollection = '%s/testfgcmcal/lut' % (instName)

        self._runPipeline(self.repo,
                          os.path.join(ROOT,
                                       'pipelines',
                                       'fgcmMakeLut%s.yaml' % (instCamel)),
                          configFiles=[configFile],
                          inputCollections='%s/calib,%s/testdata' % (instName, instName),
                          outputCollection=outputCollection,
                          registerDatasetTypes=True)

        # Check output values
        butler = dafButler.Butler(self.repo)
        lutCat = butler.get('fgcmLookUpTable',
                            collections=[outputCollection],
                            instrument=instName)
        fgcmLut, lutIndexVals, lutStd = fgcmcal.utilities.translateFgcmLut(lutCat, {})

        self.assertEqual(nBand, len(lutIndexVals[0]['FILTERNAMES']))

        indices = fgcmLut.getIndices(np.arange(nBand, dtype=np.int32),
                                     np.zeros(nBand) + np.log(lutStd[0]['PWVSTD']),
                                     np.zeros(nBand) + lutStd[0]['O3STD'],
                                     np.zeros(nBand) + np.log(lutStd[0]['TAUSTD']),
                                     np.zeros(nBand) + lutStd[0]['ALPHASTD'],
                                     np.zeros(nBand) + 1./np.cos(np.radians(lutStd[0]['ZENITHSTD'])),
                                     np.zeros(nBand, dtype=np.int32),
                                     np.zeros(nBand) + lutStd[0]['PMBSTD'])
        i0 = fgcmLut.computeI0(np.zeros(nBand) + np.log(lutStd[0]['PWVSTD']),
                               np.zeros(nBand) + lutStd[0]['O3STD'],
                               np.zeros(nBand) + np.log(lutStd[0]['TAUSTD']),
                               np.zeros(nBand) + lutStd[0]['ALPHASTD'],
                               np.zeros(nBand) + 1./np.cos(np.radians(lutStd[0]['ZENITHSTD'])),
                               np.zeros(nBand) + lutStd[0]['PMBSTD'],
                               indices)

        self.assertFloatsAlmostEqual(i0Recon, i0, msg='i0Recon', rtol=1e-5)

        i1 = fgcmLut.computeI1(np.zeros(nBand) + np.log(lutStd[0]['PWVSTD']),
                               np.zeros(nBand) + lutStd[0]['O3STD'],
                               np.zeros(nBand) + np.log(lutStd[0]['TAUSTD']),
                               np.zeros(nBand) + lutStd[0]['ALPHASTD'],
                               np.zeros(nBand) + 1./np.cos(np.radians(lutStd[0]['ZENITHSTD'])),
                               np.zeros(nBand) + lutStd[0]['PMBSTD'],
                               indices)

        self.assertFloatsAlmostEqual(i10Recon, i1/i0, msg='i10Recon', rtol=1e-5)

    def _testFgcmBuildStarsTable(self, instName, queryString, visits, nStar, nObs):
        """Test running of FgcmBuildStarsTableTask

        Parameters
        ----------
        instName : `str`
            Short name of the instrument
        queryString : `str`
            Query to send to the pipetask.
        visits : `list`
            List of visits to calibrate
        nStar : `int`
            Number of stars expected
        nObs : `int`
            Number of observations of stars expected
        """
        instCamel = instName.title()

        configFile = 'fgcmBuildStarsTable:' + os.path.join(ROOT,
                                                           'config',
                                                           'fgcmBuildStarsTable%s.py' % (instCamel))
        outputCollection = '%s/testfgcmcal/buildstars' % (instName)

        self._runPipeline(self.repo,
                          os.path.join(ROOT,
                                       'pipelines',
                                       'fgcmBuildStarsTable%s.yaml' % (instCamel)),
                          configFiles=[configFile],
                          inputCollections='%s/testfgcmcal/lut,refcats' % (instName),
                          outputCollection=outputCollection,
                          configOptions=['fgcmBuildStarsTable:ccdDataRefName=detector'],
                          queryString=queryString,
                          registerDatasetTypes=True)

        butler = dafButler.Butler(self.repo)

        visitCat = butler.get('fgcmVisitCatalog', collections=[outputCollection],
                              instrument=instName)
        self.assertEqual(len(visits), len(visitCat))

        starIds = butler.get('fgcmStarIds', collections=[outputCollection],
                             instrument=instName)
        self.assertEqual(len(starIds), nStar)

        starObs = butler.get('fgcmStarObservations', collections=[outputCollection],
                             instrument=instName)
        self.assertEqual(len(starObs), nObs)

    def _testFgcmFitCycle(self, instName, cycleNumber,
                          nZp, nGoodZp, nOkZp, nBadZp, nStdStars, nPlots,
                          skipChecks=False, extraConfig=None):
        """Test running of FgcmFitCycleTask

        Parameters
        ----------
        instName : `str`
            Short name of the instrument
        cycleNumber : `int`
            Fit cycle number.
        nZp : `int`
            Number of zeropoints created by the task
        nGoodZp : `int`
            Number of good (photometric) zeropoints created
        nOkZp : `int`
            Number of constrained zeropoints (photometric or not)
        nBadZp : `int`
            Number of unconstrained (bad) zeropoints
        nStdStars : `int`
            Number of standard stars produced
        nPlots : `int`
            Number of plots produced
        skipChecks : `bool`, optional
            Skip number checks, when running less-than-final cycle.
        extraConfig : `str`, optional
            Name of an extra config file to apply.
        """
        instCamel = instName.title()

        configFiles = ['fgcmFitCycle:' + os.path.join(ROOT,
                                                      'config',
                                                      'fgcmFitCycle%s.py' % (instCamel))]
        if extraConfig is not None:
            configFiles.append('fgcmFitCycle:' + extraConfig)

        outputCollection = '%s/testfgcmcal/fit' % (instName)

        if cycleNumber == 0:
            inputCollections = '%s/testfgcmcal/buildstars' % (instName)
        else:
            # We are reusing the outputCollection so we can't specify the input
            inputCollections = None

        cwd = os.getcwd()
        os.chdir(self.testDir)

        self._runPipeline(self.repo,
                          os.path.join(ROOT,
                                       'pipelines',
                                       'fgcmFitCycle%s.yaml' % (instCamel)),
                          configFiles=configFiles,
                          inputCollections=inputCollections,
                          outputCollection=outputCollection,
                          configOptions=['fgcmFitCycle:cycleNumber=%d' % (cycleNumber)],
                          registerDatasetTypes=True)

        os.chdir(cwd)

        if skipChecks:
            return

        butler = dafButler.Butler(self.repo)

        config = butler.get('fgcmFitCycle_config', collections=[outputCollection])

        # Check that the expected number of plots are there.
        plots = glob.glob(os.path.join(self.testDir, config.outfileBase +
                                       '_cycle%02d_plots/' % (cycleNumber) +
                                       '*.png'))
        self.assertEqual(len(plots), nPlots)

        zps = butler.get('fgcmZeropoints%d' % (cycleNumber),
                         collections=[outputCollection],
                         instrument=instName)
        self.assertEqual(len(zps), nZp)

        gd, = np.where(zps['fgcmFlag'] == 1)
        self.assertEqual(len(gd), nGoodZp)

        ok, = np.where(zps['fgcmFlag'] < 16)
        self.assertEqual(len(ok), nOkZp)

        bd, = np.where(zps['fgcmFlag'] >= 16)
        self.assertEqual(len(bd), nBadZp)

        # Check that there are no illegal values with the ok zeropoints
        test, = np.where(zps['fgcmZpt'][gd] < -9000.0)
        self.assertEqual(len(test), 0)

        stds = butler.get('fgcmStandardStars%d' % (cycleNumber),
                          collections=[outputCollection],
                          instrument=instName)

        self.assertEqual(len(stds), nStdStars)

    def _testFgcmOutputProducts(self, instName,
                                zpOffsets, testVisit, testCcd, testFilter, testBandIndex):
        """Test running of FgcmOutputProductsTask.

        Parameters
        ----------
        instName : `str`
            Short name of the instrument
        zpOffsets : `np.ndarray`
            Zeropoint offsets expected
        testVisit : `int`
            Visit id to check for round-trip computations
        testCcd : `int`
            Ccd id to check for round-trip computations
        testFilter : `str`
            Filtername for testVisit/testCcd
        testBandIndex : `int`
            Band index for testVisit/testCcd
        """
        instCamel = instName.title()

        configFile = 'fgcmOutputProducts:' + os.path.join(ROOT,
                                                          'config',
                                                          'fgcmOutputProducts%s.py' % (instCamel))
        inputCollection = '%s/testfgcmcal/fit' % (instName)
        outputCollection = '%s/testfgcmcal/fit/output' % (instName)

        self._runPipeline(self.repo,
                          os.path.join(ROOT,
                                       'pipelines',
                                       'fgcmOutputProducts%s.yaml' % (instCamel)),
                          configFiles=[configFile],
                          inputCollections=inputCollection,
                          outputCollection=outputCollection,
                          configOptions=['fgcmOutputProducts:doRefcatOutput=False'],
                          registerDatasetTypes=True)

        butler = dafButler.Butler(self.repo)
        offsetCat = butler.get('fgcmReferenceCalibrationOffsets',
                               collections=[outputCollection], instrument=instName)
        offsets = offsetCat['offset'][:]
        self.assertFloatsAlmostEqual(offsets, zpOffsets, atol=1e-6)

        config = butler.get('fgcmOutputProducts_config',
                            collections=[outputCollection], instrument=instName)

        rawStars = butler.get('fgcmStandardStars' + config.connections.cycleNumber,
                              collections=[inputCollection], instrument=instName)

        candRatio = (rawStars['npsfcand'][:, 0].astype(np.float64) /
                     rawStars['ntotal'][:, 0].astype(np.float64))
        self.assertFloatsAlmostEqual(candRatio.min(), 0.0)
        self.assertFloatsAlmostEqual(candRatio.max(), 1.0)

        # Test the fgcm_photoCalib output
        zptCat = butler.get('fgcmZeropoints' + config.connections.cycleNumber,
                            collections=[inputCollection], instrument=instName)
        selected = (zptCat['fgcmFlag'] < 16)

        # Read in all the calibrations, these should all be there
        # This test is simply to ensure that all the photoCalib files exist
        for rec in zptCat[selected]:
            testCal = butler.get('fgcm_photoCalib',
                                 visit=rec['visit'],
                                 detector=rec['detector'],
                                 collections=[outputCollection], instrument=instName)
            self.assertIsNotNone(testCal)

        # We do round-trip value checking on just the final one (chosen arbitrarily)
        testCal = butler.get('fgcm_photoCalib',
                             visit=int(testVisit), detector=int(testCcd),
                             collections=[outputCollection], instrument=instName)
        self.assertIsNotNone(testCal)

        src = butler.get('src', visit=int(testVisit), detector=int(testCcd),
                         collections=[outputCollection], instrument=instName)

        # Only test sources with positive flux
        gdSrc = (src['slot_CalibFlux_instFlux'] > 0.0)

        # We need to apply the calibration offset to the fgcmzpt (which is internal
        # and doesn't know about that yet)
        testZpInd, = np.where((zptCat['visit'] == testVisit) &
                              (zptCat['detector'] == testCcd))
        fgcmZpt = (zptCat['fgcmZpt'][testZpInd] + offsets[testBandIndex] +
                   zptCat['fgcmDeltaChrom'][testZpInd])
        fgcmZptGrayErr = np.sqrt(zptCat['fgcmZptVar'][testZpInd])

        if config.doComposeWcsJacobian:
            # The raw zeropoint needs to be modified to know about the wcs jacobian
            refs = butler.registry.queryDatasets('camera', dimensions=['instrument'],
                                                 collections=...)
            camera = butler.getDirect(list(refs)[0])
            approxPixelAreaFields = fgcmcal.utilities.computeApproxPixelAreaFields(camera)
            center = approxPixelAreaFields[testCcd].getBBox().getCenter()
            pixAreaCorr = approxPixelAreaFields[testCcd].evaluate(center)
            fgcmZpt += -2.5*np.log10(pixAreaCorr)

        # This is the magnitude through the mean calibration
        photoCalMeanCalMags = np.zeros(gdSrc.sum())
        # This is the magnitude through the full focal-plane variable mags
        photoCalMags = np.zeros_like(photoCalMeanCalMags)
        # This is the magnitude with the FGCM (central-ccd) zeropoint
        zptMeanCalMags = np.zeros_like(photoCalMeanCalMags)

        for i, rec in enumerate(src[gdSrc]):
            photoCalMeanCalMags[i] = testCal.instFluxToMagnitude(rec['slot_CalibFlux_instFlux'])
            photoCalMags[i] = testCal.instFluxToMagnitude(rec['slot_CalibFlux_instFlux'],
                                                          rec.getCentroid())
            zptMeanCalMags[i] = fgcmZpt - 2.5*np.log10(rec['slot_CalibFlux_instFlux'])

        # These should be very close but some tiny differences because the fgcm value
        # is defined at the center of the bbox, and the photoCal is the mean over the box
        self.assertFloatsAlmostEqual(photoCalMeanCalMags,
                                     zptMeanCalMags, rtol=1e-6)
        # These should be roughly equal, but not precisely because of the focal-plane
        # variation.  However, this is a useful sanity check for something going totally
        # wrong.
        self.assertFloatsAlmostEqual(photoCalMeanCalMags,
                                     photoCalMags, rtol=1e-2)

        # The next test compares the "FGCM standard magnitudes" (which are output
        # from the fgcm code itself) to the "calibrated magnitudes" that are
        # obtained from running photoCalib.calibrateCatalog() on the original
        # src catalogs.  This summary comparison ensures that using photoCalibs
        # yields the same results as what FGCM is computing internally.
        # Note that we additionally need to take into account the post-processing
        # offsets used in the tests.

        # For decent statistics, we are matching all the sources from one visit
        # (multiple ccds)
        srcRefs = butler.registry.queryDatasets('src', dimensions=['visit'],
                                                collections='%s/testdata' % (instName),
                                                where='visit = %d' % (testVisit),
                                                findFirst=True)
        photoCalibRefs = []
        for srcRef in srcRefs:
            refs = butler.registry.queryDatasets('fgcm_photoCalib',
                                                 dimensions=['visit', 'detector'],
                                                 collections=outputCollection,
                                                 where='visit = %d and detector = %d' %
                                                 (testVisit, srcRef.dataId['detector']),
                                                 findFirst=True)
            photoCalibRefs.append(list(refs)[0])

        matchMag, matchDelta = self._getMatchedVisitCat(butler, srcRefs, photoCalibRefs,
                                                        rawStars, testBandIndex, offsets)

        st = np.argsort(matchMag)
        # Compare the brightest 25% of stars.  No matter the setting of
        # deltaMagBkgOffsetPercentile, we want to ensure that these stars
        # match on average.
        brightest, = np.where(matchMag < matchMag[st[int(0.25*st.size)]])
        self.assertFloatsAlmostEqual(np.median(matchDelta[brightest]), 0.0, atol=0.002)

        # And the photoCal error is just the zeropoint gray error
        self.assertFloatsAlmostEqual(testCal.getCalibrationErr(),
                                     (np.log(10.0)/2.5)*testCal.getCalibrationMean()*fgcmZptGrayErr)

        # Test the transmission output
        visitCatalog = butler.get('fgcmVisitCatalog', collections=[inputCollection],
                                  instrument=instName)
        lutCat = butler.get('fgcmLookUpTable', collections=[inputCollection],
                            instrument=instName)

        testTrans = butler.get('transmission_atmosphere_fgcm',
                               visit=visitCatalog[0]['visit'],
                               collections=[outputCollection], instrument=instName)
        testResp = testTrans.sampleAt(position=geom.Point2D(0, 0),
                                      wavelengths=lutCat[0]['atmLambda'])

        # The test fit is performed with the atmosphere parameters frozen
        # (freezeStdAtmosphere = True).  Thus the only difference between
        # these output atmospheres and the standard is the different
        # airmass.  Furthermore, this is a very rough comparison because
        # the look-up table is computed with very coarse sampling for faster
        # testing.

        # To account for overall throughput changes, we scale by the median ratio,
        # we only care about the shape
        ratio = np.median(testResp/lutCat[0]['atmStdTrans'])
        self.assertFloatsAlmostEqual(testResp/ratio, lutCat[0]['atmStdTrans'], atol=0.04)

        # The second should be close to the first, but there is the airmass
        # difference so they aren't identical.
        testTrans2 = butler.get('transmission_atmosphere_fgcm',
                                visit=visitCatalog[1]['visit'],
                                collections=[outputCollection], instrument=instName)
        testResp2 = testTrans2.sampleAt(position=geom.Point2D(0, 0),
                                        wavelengths=lutCat[0]['atmLambda'])

        # As above, we scale by the ratio to compare the shape of the curve.
        ratio = np.median(testResp/testResp2)
        self.assertFloatsAlmostEqual(testResp/ratio, testResp2, atol=0.04)

    def _getMatchedVisitCat(self, butler, srcRefs, photoCalibRefs,
                            rawStars, bandIndex, offsets):
        """
        Get a list of matched magnitudes and deltas from calibrated src catalogs.

        Parameters
        ----------
        butler : `lsst.daf.butler.Butler`
        srcRefs : `list`
           dataRefs of source catalogs
        photoCalibRefs : `list`
           dataRefs of photoCalib files, matched to srcRefs.
        rawStars : `lsst.afw.table.SourceCatalog`
           Fgcm standard stars
        bandIndex : `int`
           Index of the band for the source catalogs
        offsets : `np.ndarray`
           Testing calibration offsets to apply to rawStars

        Returns
        -------
        matchMag : `np.ndarray`
           Array of matched magnitudes
        matchDelta : `np.ndarray`
           Array of matched deltas between src and standard stars.
        """
        matcher = esutil.htm.Matcher(11, np.rad2deg(rawStars['coord_ra']),
                                     np.rad2deg(rawStars['coord_dec']))

        matchDelta = None
        # for dataRef in dataRefs:
        for srcRef, photoCalibRef in zip(srcRefs, photoCalibRefs):
            src = butler.getDirect(srcRef)
            photoCal = butler.getDirect(photoCalibRef)

            src = photoCal.calibrateCatalog(src)

            gdSrc, = np.where(np.nan_to_num(src['slot_CalibFlux_flux']) > 0.0)

            matches = matcher.match(np.rad2deg(src['coord_ra'][gdSrc]),
                                    np.rad2deg(src['coord_dec'][gdSrc]),
                                    1./3600., maxmatch=1)

            srcMag = src['slot_CalibFlux_mag'][gdSrc][matches[0]]
            # Apply offset here to the catalog mag
            catMag = rawStars['mag_std_noabs'][matches[1]][:, bandIndex] + offsets[bandIndex]
            delta = srcMag - catMag
            if matchDelta is None:
                matchDelta = delta
                matchMag = catMag
            else:
                matchDelta = np.append(matchDelta, delta)
                matchMag = np.append(matchMag, catMag)

        return matchMag, matchDelta

    def _testFgcmCalibrateTract(self, instName, visits, tract,
                                rawRepeatability, filterNCalibMap):
        """Test running of FgcmCalibrateTractTask

        Parameters
        ----------
        instName : `str`
            Short name of the instrument
        visits : `list`
            List of visits to calibrate
        tract : `int`
            Tract number
        rawRepeatability : `np.array`
            Expected raw repeatability after convergence.
            Length should be number of bands.
        filterNCalibMap : `dict`
            Mapping from filter name to number of photoCalibs created.
        """
        instCamel = instName.title()

        configFile = os.path.join(ROOT,
                                  'config',
                                  'fgcmCalibrateTractTable%s.py' % (instCamel))

        configFiles = ['fgcmCalibrateTractTable:' + configFile]
        outputCollection = '%s/testfgcmcal/tract' % (instName)

        inputCollections = '%s/testfgcmcal/lut,refcats' % (instName)
        configOption = 'fgcmCalibrateTractTable:fgcmOutputProducts.doRefcatOutput=False'

        self._runPipeline(self.repo,
                          os.path.join(ROOT,
                                       'pipelines',
                                       'fgcmCalibrateTractTable%s.yaml' % (instCamel)),
                          configFiles=configFiles,
                          inputCollections=inputCollections,
                          outputCollection=outputCollection,
                          configOptions=[configOption],
                          registerDatasetTypes=True)

        butler = dafButler.Butler(self.repo)

        repRefs = butler.registry.queryDatasets('fgcmRawRepeatability',
                                                dimensions=['tract'],
                                                collections=outputCollection,
                                                where='tract=%d' % (tract))
        repeatabilityCat = butler.getDirect(list(repRefs)[0])
        repeatability = repeatabilityCat['rawRepeatability'][:]
        self.assertFloatsAlmostEqual(repeatability, rawRepeatability, atol=4e-6)

        # Check that the number of photoCalib objects in each filter are what we expect
        for filterName in filterNCalibMap.keys():
            whereClause = "tract=%d and physical_filter='%s'" % (tract, filterName)
            refs = butler.registry.queryDatasets('fgcm_tract_photoCalib',
                                                 dimensions=['tract', 'physical_filter'],
                                                 collections=outputCollection,
                                                 where=whereClause)
            self.assertEqual(len(set(refs)), filterNCalibMap[filterName])

        # Check that every visit got a transmission
        for visit in visits:
            whereClause = "tract=%d and visit=%d" % (tract, visit)
            refs = butler.registry.queryDatasets('transmission_atmosphere_fgcm_tract',
                                                 dimensions=['tract', 'visit'],
                                                 collections=outputCollection,
                                                 where=whereClause)
            self.assertEqual(len(set(refs)), 1)

    def tearDown(self):
        """Tear down and clear directories
        """
        if os.path.exists(self.testDir):
            shutil.rmtree(self.testDir, True)
