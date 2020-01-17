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
"""Load reference catalog objects for input to FGCM.

This task will load multi-band reference objects and apply color terms (if
configured). This wrapper around LoadReferenceObjects task also allows loading
by healpix pixel (the native pixelization of fgcm), and is self-contained so
the task can be called by third-party code.
"""

import numpy as np
import healpy as hp
from astropy import units

import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from lsst.meas.algorithms import LoadIndexedReferenceObjectsTask, ReferenceSourceSelectorTask
from lsst.meas.algorithms import getRefFluxField
from lsst.pipe.tasks.colorterms import ColortermLibrary
from lsst.afw.image import abMagErrFromFluxErr
import lsst.geom

__all__ = ['FgcmLoadReferenceCatalogConfig', 'FgcmLoadReferenceCatalogTask']


class FgcmLoadReferenceCatalogConfig(pexConfig.Config):
    """Config for FgcmLoadReferenceCatalogTask"""

    refObjLoader = pexConfig.ConfigurableField(
        target=LoadIndexedReferenceObjectsTask,
        doc="Reference object loader for photometry",
    )
    refFilterMap = pexConfig.DictField(
        doc="Mapping from camera 'filterName` to reference filter name.",
        keytype=str,
        itemtype=str,
        default={},
    )
    applyColorTerms = pexConfig.Field(
        doc=("Apply photometric color terms to reference stars?"
             "Requires that colorterms be set to a ColorTermLibrary"),
        dtype=bool,
        default=True
    )
    colorterms = pexConfig.ConfigField(
        doc="Library of photometric reference catalog name to color term dict.",
        dtype=ColortermLibrary,
    )
    referenceSelector = pexConfig.ConfigurableField(
        target=ReferenceSourceSelectorTask,
        doc="Selection of reference sources",
    )

    def validate(self):
        super().validate()
        if not self.refFilterMap:
            msg = 'Must set refFilterMap'
            raise pexConfig.FieldValidationError(FgcmLoadReferenceCatalogConfig.refFilterMap, self, msg)
        if self.applyColorTerms and len(self.colorterms.data) == 0:
            msg = "applyColorTerms=True requires the `colorterms` field be set to a ColortermLibrary."
            raise pexConfig.FieldValidationError(FgcmLoadReferenceCatalogConfig.colorterms, self, msg)


class FgcmLoadReferenceCatalogTask(pipeBase.Task):
    """
    Load multi-band reference objects from a reference catalog.

    Paramters
    ---------
    butler: `lsst.daf.persistence.Butler`
       Data butler for reading catalogs
    """
    ConfigClass = FgcmLoadReferenceCatalogConfig
    _DefaultName = 'fgcmLoadReferenceCatalog'

    def __init__(self, butler, *args, **kwargs):
        """Construct an FgcmLoadReferenceCatalogTask

        Parameters
        ----------
        butler: `lsst.daf.persistence.Buter`
           Data butler for reading catalogs.
        """
        pipeBase.Task.__init__(self, *args, **kwargs)
        self.butler = butler
        self.makeSubtask('refObjLoader', butler=butler)
        self.makeSubtask('referenceSelector')
        self._fluxFilters = None
        self._fluxFields = None
        self._referenceFilter = None

    def getFgcmReferenceStarsHealpix(self, nside, pixel, filterList, nest=False):
        """
        Get a reference catalog that overlaps a healpix pixel, using multiple
        filters.  In addition, apply colorterms if available.

        Return format is a numpy recarray for use with fgcm.

        Parameters
        ----------
        nside: `int`
           Healpix nside of pixel to load
        pixel: `int`
           Healpix pixel of pixel to load
        filterList: `list`
           list of `str` of camera filter names.
        nest: `bool`, optional
           Is the pixel in nest format?  Default is False.

        Returns
        -------
        fgcmRefCat: `np.recarray`
           Numpy recarray with the following fields:
           ra: `np.float64`
              Right ascension, degrees
           dec: `np.float64`
              Declination, degrees
           refMag: (`np.float32`, len(filterList))
              Reference magnitude for filterList bands
              Will be 99 for non-detections.
           refMagErr: (`np.float32`, len(filterList))
              Reference magnitude error for filterList bands
              Will be 99 for non-detections.
        """

        # Determine the size of the sky circle to load
        theta, phi = hp.pix2ang(nside, pixel, nest=nest)
        center = lsst.geom.SpherePoint(phi * lsst.geom.radians, (np.pi/2. - theta) * lsst.geom.radians)

        corners = hp.boundaries(nside, pixel, step=1, nest=nest)
        theta_phi = hp.vec2ang(np.transpose(corners))

        radius = 0.0 * lsst.geom.radians
        for ctheta, cphi in zip(*theta_phi):
            rad = center.separation(lsst.geom.SpherePoint(cphi * lsst.geom.radians,
                                                          (np.pi/2. - ctheta) * lsst.geom.radians))
            if (rad > radius):
                radius = rad

        # Load the fgcm-format reference catalog
        fgcmRefCat = self.getFgcmReferenceStarsSkyCircle(center.getRa().asDegrees(),
                                                         center.getDec().asDegrees(),
                                                         radius.asDegrees(),
                                                         filterList)
        catPix = hp.ang2pix(nside, np.radians(90.0 - fgcmRefCat['dec']),
                            np.radians(fgcmRefCat['ra']), nest=nest)

        inPix, = np.where(catPix == pixel)

        return fgcmRefCat[inPix]

    def getFgcmReferenceStarsSkyCircle(self, ra, dec, radius, filterList):
        """
        Get a reference catalog that overlaps a circular sky region, using
        multiple filters.  In addition, apply colorterms if available.

        Return format is a numpy recarray for use with fgcm.

        Parameters
        ----------
        ra: `float`
           ICRS right ascension, degrees.
        dec: `float`
           ICRS declination, degrees.
        radius: `float`
           Radius to search, degrees.
        filterList: `list`
           list of `str` of camera filter names.

        Returns
        -------
        fgcmRefCat: `np.recarray`
           Numpy recarray with the following fields:
           ra: `np.float64`
              Right ascension, degrees
           dec: `np.float64`
              Declination, degrees
           refMag: (`np.float32`, len(filterList))
              Reference magnitude for filterList bands
              Will be 99 for non-detections.
           refMagErr: (`np.float32`, len(filterList))
              Reference magnitude error for filterList bands
              Will be 99 for non-detections.
        """

        center = lsst.geom.SpherePoint(ra * lsst.geom.degrees, dec * lsst.geom.degrees)

        # Check if we haev previously cached values for the fluxFields
        if self._fluxFilters is None or self._fluxFilters != filterList:
            self._determine_flux_fields(center, filterList)

        skyCircle = self.refObjLoader.loadSkyCircle(center,
                                                    radius * lsst.geom.degrees,
                                                    self._referenceFilter)

        if not skyCircle.refCat.isContiguous():
            refCat = skyCircle.refCat.copy(deep=True)
        else:
            refCat = skyCircle.refCat

        # Select on raw (uncorrected) catalog, where the errors should make more sense
        goodSources = self.referenceSelector.selectSources(refCat)
        selected = goodSources.selected

        fgcmRefCat = np.zeros(np.sum(selected), dtype=[('ra', 'f8'),
                                                       ('dec', 'f8'),
                                                       ('refMag', 'f4', len(filterList)),
                                                       ('refMagErr', 'f4', len(filterList))])
        if fgcmRefCat.size == 0:
            # Return an empty catalog if we don't have any selected sources
            return fgcmRefCat

        # The ra/dec native Angle format is radians
        # We determine the conversion from the native units (typically
        # radians) to degrees for the first observation.  This allows us
        # to treate ra/dec as numpy arrays rather than Angles, which would
        # be approximately 600x slower.

        conv = refCat[0]['coord_ra'].asDegrees() / float(refCat[0]['coord_ra'])
        fgcmRefCat['ra'] = refCat['coord_ra'][selected] * conv
        fgcmRefCat['dec'] = refCat['coord_dec'][selected] * conv

        # Default (unset) values are 99.0
        fgcmRefCat['refMag'][:, :] = 99.0
        fgcmRefCat['refMagErr'][:, :] = 99.0

        if self.config.applyColorTerms:
            try:
                refCatName = self.refObjLoader.ref_dataset_name
            except AttributeError:
                # NOTE: we need this try:except: block in place until we've
                # completely removed a.net support
                raise RuntimeError("Cannot perform colorterm corrections with a.net refcats.")

            for i, (filterName, fluxField) in enumerate(zip(self._fluxFilters, self._fluxFields)):
                if fluxField is None:
                    continue

                self.log.debug("Applying color terms for filtername=%r" % (filterName))

                colorterm = self.config.colorterms.getColorterm(
                    filterName=filterName, photoCatName=refCatName, doRaise=True)

                refMag, refMagErr = colorterm.getCorrectedMagnitudes(refCat, filterName)

                # nan_to_num replaces nans with zeros, and this ensures that we select
                # magnitudes that both filter out nans and are not very large (corresponding
                # to very small fluxes), as "99" is a common sentinel for illegal magnitudes.

                good, = np.where((np.nan_to_num(refMag[selected]) < 90.0) &
                                 (np.nan_to_num(refMagErr[selected]) < 90.0) &
                                 (np.nan_to_num(refMagErr[selected]) > 0.0))

                fgcmRefCat['refMag'][good, i] = refMag[selected][good]
                fgcmRefCat['refMagErr'][good, i] = refMagErr[selected][good]

        else:
            # No colorterms

            # TODO: need to use Jy here until RFC-549 is completed and refcats return nanojansky

            for i, (filterName, fluxField) in enumerate(zip(self._fluxFilters, self._fluxFields)):
                # nan_to_num replaces nans with zeros, and this ensures that we select
                # fluxes that both filter out nans and are positive.
                good, = np.where((np.nan_to_num(refCat[fluxField][selected]) > 0.0) &
                                 (np.nan_to_num(refCat[fluxField+'Err'][selected]) > 0.0))
                refMag = (refCat[fluxField][selected][good] * units.Jy).to_value(units.ABmag)
                refMagErr = abMagErrFromFluxErr(refCat[fluxField+'Err'][selected][good],
                                                refCat[fluxField][selected][good])
                fgcmRefCat['refMag'][good, i] = refMag
                fgcmRefCat['refMagErr'][good, i] = refMagErr

        return fgcmRefCat

    def _determine_flux_fields(self, center, filterList):
        """
        Determine the flux field names for a reference catalog.

        Will set self._fluxFields, self._referenceFilter.

        Parameters
        ----------
        center: `lsst.geom.SpherePoint`
           The center around which to load test sources.
        filterList: `list`
           list of `str` of camera filter names.
        """

        # Record self._fluxFilters for checks on subsequent calls
        self._fluxFilters = filterList

        # Search for a good filter to use to load the reference catalog
        # via the refObjLoader task which requires a valid filterName
        foundReferenceFilter = False
        for filterName in filterList:
            try:
                refFilterName = self.config.refFilterMap[filterName]
            except KeyError:
                # It is okay to not have a match in the reference catalog, but warn
                self.log.warn(f'Camera filter {filterName} not defined in refFilterMap')
                # Go to the next filterName
                continue

            try:
                results = self.refObjLoader.loadSkyCircle(center,
                                                          0.05 * lsst.geom.degrees,
                                                          refFilterName)
                foundReferenceFilter = True
                self._referenceFilter = refFilterName
                break
            except RuntimeError:
                # This just means that the filterName wasn't listed
                # in the reference catalog.  This is okay.
                pass

        if not foundReferenceFilter:
            raise RuntimeError("Could not find any valid flux field(s) %s" %
                               (", ".join(filterList)))

        # Retrieve all the fluxField names
        self._fluxFields = []
        for filterName in filterList:
            try:
                refFilterName = self.config.refFilterMap[filterName]
            except KeyError:
                refFilterName = None
                fluxField = None

            if refFilterName is not None:
                try:
                    fluxField = getRefFluxField(results.refCat.schema, filterName=refFilterName)
                except RuntimeError:
                    # This flux field isn't available.  Set to None
                    fluxField = None

            if fluxField is None:
                self.log.warn(f'No reference flux field for camera filter {filterName}')

            self._fluxFields.append(fluxField)
