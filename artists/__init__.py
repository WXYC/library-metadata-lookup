"""Artist-search-alias endpoint.

`POST /api/v1/artists/search-aliases/bulk` returns the per-source variants
the composer can collect from `entity.identity` + the discogs-cache for a
batch of WXYC canonical artist names. See `composer.py` for the two-phase
compose and `router.py` for the HTTP surface.
"""
