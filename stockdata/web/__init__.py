"""Local web application: job queue, supervisor, data explorer and quality views around the existing
``stockdata`` download engine. It adds no second downloader: every download is the normal
``python -m stockdata download ...`` command run in a supervised worker process."""
