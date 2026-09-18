"""Bottle Net - Video Publisher.

Uploads and schedules videos to YouTube (YouTube Data API) and Facebook
Pages (Meta Graph API). Accounts are connected with the platforms' official
OAuth flows only: Bottle Net never sees or stores a Google or Facebook
password.

Layout:

* ``storage``/``settings``/``paths`` - SQLite persistence and settings
* ``jobs`` - creating, editing, cancelling and retrying upload jobs
* ``queue`` - runs one platform upload (with retries)
* ``scheduler`` - runs due jobs, survives restarts, handles missed jobs
* ``youtube``/``facebook`` - OAuth and upload clients per platform
* ``library``/``downloads`` - the video library and downloader integration
* ``server`` + ``web/`` - the local web GUI (``bottle-net gui``)
"""
