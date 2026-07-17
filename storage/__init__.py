"""Object-storage abstraction for LML's two hosted SQLite artifacts.

``library.db`` and ``streaming_availability.db`` are moved off the Railway
volume into a Railway Bucket (S3-compatible object storage) so deploys become
zero-downtime and the service becomes replica-ready. This package holds the
storage seam; the two files' boot-fetch and upload paths route through it.

See the volume-eviction epic (WXYC/library-metadata-lookup#834).
"""
