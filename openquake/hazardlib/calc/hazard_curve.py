# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2012-2016 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

"""
:mod:`openquake.hazardlib.calc.hazard_curve` implements
:func:`hazard_curves`.
"""
import sys
import time
import collections

import numpy

from openquake.baselib.python3compat import raise_, zip
from openquake.baselib.performance import Monitor
from openquake.hazardlib.poe import PoeCurve, Imtls
from openquake.hazardlib.calc import filters
from openquake.hazardlib.gsim.base import ContextMaker, FarAwayRupture
from openquake.hazardlib.imt import from_string
from openquake.baselib.general import deprecated


def zero_curves(num_sites, imtls):
    """
    :param num_sites: the number of sites
    :param imtls: the intensity measure levels dictionary
    :returns: an array of zero curves with length num_sites
    """
    # numpy dtype for the hazard curves
    imt_dt = numpy.dtype([(imt, float, 1 if imls is None else len(imls))
                          for imt, imls in imtls.items()])
    zero = numpy.zeros(num_sites, imt_dt)
    return zero


def zero_maps(num_sites, imts, poes=()):
    """
    :param num_sites: the number of sites
    :param imts: the intensity measure types
    :returns: an array of zero curves with length num_sites
    """
    # numpy dtype for the hazard maps
    if poes:
        imt_dt = numpy.dtype([('%s~%s' % (imt, poe), numpy.float32)
                              for imt in imts for poe in poes])
    else:
        imt_dt = numpy.dtype([(imt, numpy.float32) for imt in imts])
    return numpy.zeros(num_sites, imt_dt)


def agg_curves(acc, curves):
    """
    Aggregate hazard curves by composing the probabilities.

    :param acc: an accumulator array
    :param curves: an array of hazard curves
    :returns: a new accumulator
    """
    new = numpy.array(acc)  # copy of the accumulator
    for imt in curves.dtype.fields:
        new[imt] = 1. - (1. - curves[imt]) * (1. - acc[imt])
    return new


@deprecated('Use calc_hazard_curves instead')
def hazard_curves(
        sources, sites, imtls, gsim_by_trt, truncation_level=None,
        source_site_filter=filters.source_site_noop_filter):
    """
    Deprecated. It does the same job of
    :func:`openquake.hazardlib.calc.hazard_curve.calc_hazard_curves`,
    with the only difference that the intensity measure types in input
    and output are hazardlib objects instead of simple strings.
    """
    imtls = {str(imt): imls for imt, imls in imtls.items()}
    curves_by_imt = calc_hazard_curves(
        sources, sites, imtls, gsim_by_trt, truncation_level,
        source_site_filter=filters.source_site_noop_filter)
    return {from_string(imt): curves_by_imt[imt] for imt in imtls}


def calc_hazard_curves(
        sources, sites, imtls, gsim_by_trt, truncation_level=None,
        source_site_filter=filters.source_site_noop_filter,
        maximum_distance=None):
    """
    Compute hazard curves on a list of sites, given a set of seismic sources
    and a set of ground shaking intensity models (one per tectonic region type
    considered in the seismic sources).


    Probability of ground motion exceedance is computed using the following
    formula ::

        P(X≥x|T) = 1 - ∏ ∏ Prup_ij(X<x|T)

    where ``P(X≥x|T)`` is the probability that the ground motion parameter
    ``X`` is exceeding level ``x`` one or more times in a time span ``T``, and
    ``Prup_ij(X<x|T)`` is the probability that the j-th rupture of the i-th
    source is not producing any ground motion exceedance in time span ``T``.
    The first product ``∏`` is done over sources, while the second one is done
    over ruptures in a source.

    The above formula computes the probability of having at least one ground
    motion exceedance in a time span as 1 minus the probability that none of
    the ruptures in none of the sources is causing a ground motion exceedance
    in the same time span. The basic assumption is that seismic sources are
    independent, and ruptures in a seismic source are also independent.

    :param sources:
        A sequence of seismic sources objects (instances of subclasses
        of :class:`~openquake.hazardlib.source.base.BaseSeismicSource`).
    :param sites:
        Instance of :class:`~openquake.hazardlib.site.SiteCollection` object,
        representing sites of interest.
    :param imtls:
        Dictionary mapping intensity measure type strings
        to lists of intensity measure levels.
    :param gsim_by_trt:
        Dictionary mapping tectonic region types (members
        of :class:`openquake.hazardlib.const.TRT`) to
        :class:`~openquake.hazardlib.gsim.base.GMPE` or
        :class:`~openquake.hazardlib.gsim.base.IPE` objects.
    :param truncation_level:
        Float, number of standard deviations for truncation of the intensity
        distribution.
    :param source_site_filter:
        Optional source-site filter function. See
        :mod:`openquake.hazardlib.calc.filters`.

    :returns:
        An array of size N, where N is the number of sites, which elements
        are records with fields given by the intensity measure types; the
        size of each field is given by the number of levels in ``imtls``.
    """
    sources_by_trt = collections.defaultdict(list)
    for src in sources:
        sources_by_trt[src.tectonic_region_type].append(src)
    acc = {}
    for trt in sources_by_trt:
        acc = PoeCurve.compose(acc, hazard_curves_per_trt(
            sources_by_trt[trt], sites, imtls, [gsim_by_trt[trt]],
            truncation_level, source_site_filter))
    return acc2curves(acc, 1, len(sites), imtls)[0]  # there is a single GSIM


def acc2curves(acc, ngsims, nsites, imtls):
    curves = zero_curves((ngsims, nsites), imtls)
    for sid in acc:
        for j in range(ngsims):
            curves[j, sid] = acc[sid].array[j].view(curves.dtype)
    return curves


def hazard_curves_per_trt(
        sources, sites, imtls, gsims, truncation_level=None,
        source_site_filter=filters.source_site_noop_filter,
        maximum_distance=None, bbs=(), monitor=Monitor()):
    """
    Compute the hazard curves for a set of sources belonging to the same
    tectonic region type for all the GSIMs associated to that TRT.
    The arguments are the same as in :func:`calc_hazard_curves`, except
    for ``gsims``, which is a list of GSIM instances.

    :returns:
        A list of G arrays of size N, where N is the number of sites and
        G the number of gsims. Each array contains records with fields given
        by the intensity measure types; the size of each field is given by the
        number of levels in ``imtls``.
    """
    cmaker = ContextMaker(gsims, maximum_distance)
    imtls = Imtls(imtls)
    sources_sites = ((source, sites) for source in sources)
    ctx_mon = monitor('making contexts', measuremem=False)
    pne_mon = monitor('computing poes', measuremem=False)
    monitor.calc_times = []  # pairs (src_id, delta_t)
    monitor.eff_ruptures = 0  # effective number of contributing ruptures
    acc = {}
    for source, s_sites in source_site_filter(sources_sites):
        t0 = time.time()
        curves = PoeCurve.build(imtls, gsims, s_sites.indices, initvalue=1.)
        try:
            for rupture in source.iter_ruptures():
                with ctx_mon:
                    try:
                        sctx, rctx, dctx = cmaker.make_contexts(
                            s_sites, rupture)
                    except FarAwayRupture:
                        continue

                    monitor.eff_ruptures += 1

                    # add optional disaggregation information (bounding boxes)
                    if bbs:
                        sids = set(sctx.sites.sids)
                        jb_dists = dctx.rjb
                        closest_points = rupture.surface.get_closest_points(
                            sctx.sites.mesh)
                        bs = [bb for bb in bbs if bb.site_id in sids]
                        # NB: the assert below is always true; we are
                        # protecting against possible refactoring errors
                        assert len(bs) == len(jb_dists) == len(closest_points)
                        for bb, dist, p in zip(bs, jb_dists, closest_points):
                            if dist < maximum_distance:
                                # rbuptures too far away are ignored
                                bb.update([dist], [p.longitude], [p.latitude])

                with pne_mon:
                    pnes = get_probability_no_exceedance(
                        rupture, sctx, rctx, dctx, imtls, gsims,
                        truncation_level)
                    for sid, pne in zip(sctx.sites.indices, pnes):
                        curves[sid].array *= pne
        except Exception as err:
            etype, err, tb = sys.exc_info()
            msg = 'An error occurred with source id=%s. Error: %s'
            msg %= (source.source_id, str(err))
            raise_(etype, msg, tb)

        # we are attaching the calculation times to the monitor
        # so that oq-lite (and the engine) can store them
        monitor.calc_times.append((source.id, time.time() - t0))
        # NB: source.id is an integer; it should not be confused
        # with source.source_id, which is a string
        acc = PoeCurve.compose(acc, {sid: ~curves[sid] for sid in curves})
    return acc  # sid -> PoeCurve


# NB: it is important for this to be fast since it is inside an inner loop
def get_probability_no_exceedance(
        rupture, sctx, rctx, dctx, imtls, gsims, trunclevel):
    """
    :param rupture: a Rupture instance
    :param sctx: the corresponding SiteContext instance
    :param rctx: the corresponding RuptureContext instance
    :param dctx: the corresponding DistanceContext instance
    :param gsims: the list of GSIMs to use
    :param trunclevel: the truncation level
    :returns: an array of shape (num_sites, num_gsim, num_levels)
    """
    pne_array = numpy.zeros((len(sctx.sites), len(gsims), len(imtls.array)))
    for i, gsim in enumerate(gsims):
        pnos = []  # list of arrays nsites x nlevels
        for imt in imtls:
            poes = gsim.get_poes(
                sctx, rctx, dctx, from_string(imt), imtls[imt], trunclevel)
            pnos.append(rupture.get_probability_no_exceedance(poes))
        pne_array[:, i, :] = numpy.concatenate(pnos, axis=1)
    return pne_array
