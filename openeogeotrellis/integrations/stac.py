from typing import (
    Dict,
    Optional,
)
from urllib.error import HTTPError

from pystac.stac_io import DefaultStacIO, _is_url
from urllib3 import Retry, PoolManager


class StacApiIO(DefaultStacIO):

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        retry: Optional[Retry] = None,
     ):
        super().__init__(headers=headers)
        self.timeout = timeout or 20
        self.retry = retry or Retry()

    def read_text_from_href(self, href: str) -> str:
        """Reads file as a UTF-8 string, with retry and timeout support.

        Args:
            href : The URI of the file to open.
        """
        if _is_url(href):
            http = PoolManager(retries=self.retry, timeout=20)
            try:
                response = http.request(
                    "GET", href
                )
                return response.data.decode("utf-8")
            except HTTPError as e:
                raise Exception("Could not read uri {}".format(href)) from e
        else:
            return super().read_text_from_href(href)
