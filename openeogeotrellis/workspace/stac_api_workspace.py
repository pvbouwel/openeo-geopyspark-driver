from pathlib import PurePath, Path
from typing import Union, Callable, Tuple
from urllib.error import HTTPError

from openeo_driver.util.http import requests_with_retry
from openeo_driver.workspace import Workspace, _merge_collection_metadata
from pystac import STACObject, Collection, Item, Asset
import pystac_client
from pystac_client import ConformanceClasses
from requests import Session


class StacApiWorkspace(Workspace):
    def __init__(
        self,
        root_url: str,
        # (asset, remove_original) => (alternate ID, workspace URI)
        export_asset: Callable[[Asset, bool], Tuple[str, str]],
        additional_collection_properties=None,
        get_access_token: Callable[[], str] = None,
    ):
        """
        :param root_url: the URL to the STAC API's root catalog
        :param additional_collection_properties: top-level Collection properties to include in the request
        :param get_access_token: supply an access token, if needed
        :param export_asset: copy/move an asset and return its workspace URI as an alternate

        Re: export_asset:
        * locally with assets on disk: possibly copy to a persistent directory and adapt href
        * Terrascope: possibly copy to a public directory and adapt href
        * CDSE: possibly translate file path to s3:// URI and adapt href
        """

        if additional_collection_properties is None:
            additional_collection_properties = {}

        self.root_url = root_url
        self._additional_collection_properties = additional_collection_properties
        self._get_access_token = get_access_token
        self._export_asset = export_asset

    def import_file(self, common_path: Union[str, Path], file: Path, merge: str, remove_original: bool = False) -> str:
        raise NotImplementedError

    def import_object(
        self, common_path: Union[str, Path], s3_uri: str, merge: str, remove_original: bool = False
    ) -> str:
        raise NotImplementedError

    def merge(self, stac_resource: STACObject, target: PurePath, remove_original: bool = False) -> STACObject:
        self._assert_catalog_supports_necessary_api()

        stac_resource = stac_resource.full_copy()
        collection_id = str(target)
        del target

        if isinstance(stac_resource, Collection):
            new_collection = stac_resource

            existing_collection = None
            try:
                existing_collection = Collection.from_file(f"{self.root_url}/collections/{collection_id}")
            except Exception as e:
                if self._is_not_found_error(e):
                    pass  # not exceptional: the target collection does not exist yet
                else:
                    raise

            with requests_with_retry() as session:
                # TODO: uses a single access token for the collection + all items
                session.headers = (
                    {"Authorization": f"Bearer {self._get_access_token()}"} if self._get_access_token else None
                )

                merged_collection = (
                    _merge_collection_metadata(existing_collection, new_collection) if existing_collection
                    else new_collection
                )

                self._upload_collection(
                    merged_collection,
                    collection_id,
                    modify_existing=bool(existing_collection),
                    session=session,
                )

                for new_item in new_collection.get_items():
                    new_item.make_asset_hrefs_absolute()  # probably makes sense for a STAC API
                    self._upload_item(new_item, collection_id, session)

            for new_item in new_collection.get_items():
                for asset in new_item.assets.values():
                    alternate_id, workspace_uri = self._export_asset(asset.clone(), remove_original)
                    asset.extra_fields["alternate"] = {alternate_id: workspace_uri}

            return new_collection
        else:
            raise NotImplementedError(f"merge from {stac_resource}")

    def _upload_collection(self, collection: Collection, collection_id: str, modify_existing: bool, session: Session):
        bare_collection = collection.clone()
        bare_collection.id = collection_id
        bare_collection.remove_hierarchical_links()
        bare_collection.extra_fields.update(self._additional_collection_properties)

        request_json = bare_collection.to_dict(include_self_link=False)

        if modify_existing:
            resp = session.put(
                f"{self.root_url}/collections/{collection_id}",
                json=request_json,
            )
        else:
            resp = session.post(
                f"{self.root_url}/collections",
                json=request_json,
            )

        resp.raise_for_status()

    def _upload_item(self, item: Item, collection_id: str, session: Session):
        item.remove_hierarchical_links()
        item.collection_id = collection_id

        resp = session.post(
            f"{self.root_url}/collections/{collection_id}/items",
            json=item.to_dict(include_self_link=False),
        )
        resp.raise_for_status()

    def _assert_catalog_supports_necessary_api(self):
        root_catalog_client = pystac_client.Client.open(self.root_url)

        if not root_catalog_client.conforms_to(ConformanceClasses.COLLECTIONS):
            raise ValueError(f"{self.root_url} does not support Collections")

        conforms_to = root_catalog_client.get_conforms_to()

        supports_collection_methods = any(
            conformance_class.endswith("/collections/extensions/transaction") for conformance_class in conforms_to
        )

        if not supports_collection_methods:
            raise ValueError(f"{self.root_url} does not support Transaction extension for Collections")

        supports_item_methods = any(
            conformance_class.endswith("/ogcapi-features/extensions/transaction") for conformance_class in conforms_to
        )

        if not supports_item_methods:
            raise ValueError(f"{self.root_url} does not support Transaction extension for Items")

    def _is_not_found_error(self, e: BaseException) -> bool:
        return (isinstance(e, HTTPError) and e.code == 404) or (
            e.__cause__ is not None and self._is_not_found_error(e.__cause__)
        )