"""Advisory-only forecast provider adapters (ARCHITECTURE sections 13 and 17).

The reserved provider families live here: tariff, weather, PV forecast, and
load forecast.  Every member normalizes onto :mod:`energypod.adapters.providers.model`
and nothing in this package is reachable from a control path -- the kernel,
arbiter, safety evaluation, and actors never import it.  Advisers and
projections are the only consumers.
"""
