import datetime as dt
import logging
from typing import Union, Optional, Tuple, Dict, List, Iterable
from urllib.parse import urlparse

import dateutil
import geopyspark as gps
import pystac
import pystac_client
from geopyspark import LayerType, TiledRasterLayer
from openeo.metadata import SpatialDimension, TemporalDimension, BandDimension, Band
from openeo.util import rfc3339
from openeo_driver import filter_properties, backend
from openeo_driver.backend import LoadParameters, BatchJobMetadata
from openeo_driver.errors import OpenEOApiException, ProcessParameterUnsupportedException, JobNotFoundException, \
    ProcessParameterInvalidException
from openeo_driver.users import User
from openeo_driver.util.geometry import BoundingBox, GeometryBufferer
from openeo_driver.util.utm import utm_zone_from_epsg
from openeo_driver.utils import EvalEnv
from shapely.geometry import Polygon

from openeogeotrellis.geopysparkcubemetadata import GeopysparkCubeMetadata
from openeogeotrellis.geopysparkdatacube import GeopysparkDataCube
from openeogeotrellis.utils import normalize_temporal_extent, get_jvm, to_projected_polygons

logger = logging.getLogger(__name__)


def load_stac(url: str, load_params: LoadParameters, env: EvalEnv, layer_properties: Dict[str, object],
              batch_jobs: Optional[backend.BatchJobs]) -> GeopysparkDataCube:
    logger.info("load_stac from url {u!r} with load params {p!r}".format(u=url, p=load_params))

    no_data_available_exception = OpenEOApiException(message="There is no data available for the given extents.",
                                                     code="NoDataAvailable", status_code=400)
    properties_unsupported_exception = ProcessParameterUnsupportedException("load_stac", "properties")

    all_properties = {**layer_properties, **load_params.properties}

    user: Union[User, None] = env["user"]

    requested_bbox = BoundingBox.from_dict_or_none(
        load_params.spatial_extent, default_crs="EPSG:4326"
    )

    temporal_extent = load_params.temporal_extent
    from_date, until_date = map(dt.datetime.fromisoformat, normalize_temporal_extent(temporal_extent))
    to_date = (dt.datetime.combine(until_date, dt.time.max, until_date.tzinfo) if from_date == until_date
               else until_date - dt.timedelta(milliseconds=1))

    def intersects_spatiotemporally(itm: pystac.Item) -> bool:
        def intersects_temporally() -> bool:
            nominal_date = itm.datetime or dateutil.parser.parse(itm.properties["start_datetime"])
            return from_date <= nominal_date <= to_date

        def intersects_spatially() -> bool:
            if not requested_bbox or itm.bbox is None:
                return True

            requested_bbox_lonlat = requested_bbox.reproject("EPSG:4326")
            return requested_bbox_lonlat.as_polygon().intersects(
                Polygon.from_bounds(*itm.bbox)
            )

        return intersects_temporally() and intersects_spatially()

    def supports_item_search(coll: pystac.Collection) -> bool:
        # TODO: use pystac_client instead?
        conforms_to = coll.get_root().extra_fields.get("conformsTo", [])
        return any(conformance_class.endswith("/item-search") for conformance_class in conforms_to)

    def is_band_asset(asset: pystac.Asset) -> bool:
        return "eo:bands" in asset.extra_fields

    def get_band_names(itm: pystac.Item, asst: pystac.Asset) -> List[str]:
        def get_band_name(eo_band) -> str:
            if isinstance(eo_band, dict):
                return eo_band["name"]

            # can also be an index into a list of bands elsewhere.
            # TODO: still necessary to support this? See https://github.com/Open-EO/openeo-geopyspark-driver/issues/619
            assert isinstance(eo_band, int)
            eo_band_index = eo_band

            eo_bands_location = (itm.properties if "eo:bands" in itm.properties
                                 else itm.get_collection().summaries.to_dict())
            return get_band_name(eo_bands_location["eo:bands"][eo_band_index])

        return [get_band_name(eo_band) for eo_band in asst.extra_fields["eo:bands"]]

    def get_proj_metadata(itm: pystac.Item, asst: pystac.Asset) -> (Optional[int],
                                                                    Optional[Tuple[float, float, float, float]],
                                                                    Optional[Tuple[int, int]]):
        """Returns EPSG code, bbox (in that EPSG) and number of pixels (rows, cols), if available."""
        epsg = asst.extra_fields.get("proj:epsg") or itm.properties.get("proj:epsg")
        bbox = asst.extra_fields.get("proj:bbox") or itm.properties.get("proj:bbox")
        shape = asst.extra_fields.get("proj:shape") or itm.properties.get("proj:shape")
        return (epsg,
                tuple(map(float, bbox)) if bbox else None,
                tuple(shape) if shape else None)

    def matches_metadata_properties(itm: pystac.Item) -> bool:
        literal_matches = {property_name: filter_properties.extract_literal_match(condition)
                           for property_name, condition in all_properties.items()}

        def operator_value(criterion: Dict[str, object]) -> (str, object):
            if len(criterion) != 1:
                raise ValueError(f'expected a single criterion, was {criterion}')

            (operator, value), = criterion.items()
            return operator, value

        for property_name, criterion in literal_matches.items():
            if property_name not in itm.properties:
                return False

            item_value = itm.properties[property_name]
            operator, criterion_value = operator_value(criterion)

            if operator == 'eq' and item_value != criterion_value:
                return False
            if operator == 'lte' and item_value is not None and item_value > criterion_value:
                return False
            if operator == 'gte' and item_value is not None and item_value < criterion_value:
                return False

        return True

    collection = None

    # TODO: `user` might be None
    dependency_job_info = (extract_own_job_info(url, user_id=user.user_id, batch_jobs=batch_jobs) if batch_jobs
                           else None)

    if dependency_job_info:
        intersecting_items = []

        for asset_id, asset in batch_jobs.get_result_assets(job_id=dependency_job_info.id,
                                                            user_id=user.user_id).items():
            pystac_item = pystac.Item(id=asset_id, geometry=asset["geometry"], bbox=asset["bbox"],
                                      datetime=rfc3339.parse_datetime(asset["datetime"], with_timezone=True),
                                      properties={"datetime": asset["datetime"]})

            if intersects_spatiotemporally(pystac_item) and "data" in asset.get("roles", []):
                eo_bands = [{"name": b.name} for b in asset["bands"]]
                pystac_asset = pystac.Asset(href=asset["href"], extra_fields={"eo:bands": eo_bands})
                pystac_item.add_asset(asset_id, pystac_asset)
                intersecting_items.append(pystac_item)

        band_names = []
    else:
        stac_object = pystac.read_file(href=url)

        if isinstance(stac_object, pystac.Item):
            if load_params.properties:
                raise properties_unsupported_exception

            item = stac_object

            if not intersects_spatiotemporally(item):
                raise no_data_available_exception

            if "eo:bands" in item.properties:
                eo_bands_location = item.properties
            elif item.get_collection() is not None:
                collection = item.get_collection()
                eo_bands_location = item.get_collection().summaries.lists
            else:
                # TODO: band order is not "stable" here, see https://github.com/Open-EO/openeo-processes/issues/488
                eo_bands_location = {}
            band_names = [b["name"] for b in eo_bands_location.get("eo:bands", [])]

            intersecting_items = [item]
        elif isinstance(stac_object, pystac.Collection) and supports_item_search(stac_object):
            collection = stac_object
            collection_id = collection.id

            root_catalog = collection.get_root()

            band_names = [b["name"] for b in collection.summaries.lists.get("eo:bands", [])]

            client = pystac_client.Client.open(root_catalog.get_self_href())

            if root_catalog.get_self_href().startswith("https://tamn.snapplanet.io"):
                # by default, returns all properties and "none" if fields is specified
                fields = None
            else:
                # standard behavior seems to be to include only a minimal subset e.g. https://stac.openeo.vito.be/
                fields = [f"properties.{property_name}" for property_name in all_properties.keys()]

            search_request = client.search(
                method="GET",
                collections=collection_id,
                bbox=requested_bbox.reproject("EPSG:4326").as_wsen_tuple() if requested_bbox else None,
                limit=20,
                datetime=f"{from_date.isoformat().replace('+00:00', 'Z')}/"
                         f"{to_date.isoformat().replace('+00:00', 'Z')}",  # end is inclusive
                fields=fields,
            )

            logger.info(f"STAC API request: GET {search_request.url_with_parameters()}")

            # TODO: use server-side filtering as well (at least STAC API Filter Extension)
            intersecting_items = filter(lambda itm: matches_metadata_properties(itm), search_request.items())
        else:
            assert isinstance(stac_object, pystac.Catalog)  # static Catalog + Collection
            catalog = stac_object

            if load_params.properties:
                raise properties_unsupported_exception

            if isinstance(catalog, pystac.Collection):
                collection = catalog

            band_names = [b["name"] for b in (catalog.summaries.lists if isinstance(catalog, pystac.Collection)
                                              else catalog.extra_fields.get("summaries", {})).get("eo:bands", [])]

            def intersecting_catalogs(root: pystac.Catalog) -> Iterable[pystac.Catalog]:
                def intersects_spatiotemporally(coll: pystac.Collection) -> bool:
                    def intersects_spatially(bbox) -> bool:
                        if not requested_bbox:
                            return True

                        requested_bbox_lonlat = requested_bbox.reproject("EPSG:4326")
                        return requested_bbox_lonlat.as_polygon().intersects(
                            Polygon.from_bounds(*bbox)
                        )

                    def intersects_temporally(interval) -> bool:
                        start, end = interval

                        if start is not None and end is not None:
                            return to_date >= start and from_date <= end
                        if start is not None:
                            return to_date >= start
                        if end is not None:
                            return from_date <= end
                        return True

                    bboxes = coll.extent.spatial.bboxes
                    intervals = coll.extent.temporal.intervals

                    if len(bboxes) > 1 and not any(intersects_spatially(bbox) for bbox in bboxes[1:]):
                        return False
                    if len(bboxes) == 1 and not intersects_spatially(bboxes[0]):
                        return False

                    if len(intervals) > 1 and not any(intersects_temporally(interval)
                                                      for interval in intervals[1:]):
                        return False
                    if len(intervals) == 1 and not intersects_temporally(intervals[0]):
                        return False

                    return True

                if isinstance(root, pystac.Collection) and not intersects_spatiotemporally(root):
                    return []

                yield root
                for child in root.get_children():
                    yield from intersecting_catalogs(child)

            intersecting_items = (itm
                                  for intersecting_catalog in intersecting_catalogs(root=catalog)
                                  for itm in intersecting_catalog.get_items() if intersects_spatiotemporally(itm))

    jvm = get_jvm()

    opensearch_client = jvm.org.openeo.geotrellis.file.FixedFeaturesOpenSearchClient()

    stac_bbox = None
    items_found = False
    proj_epsg = None
    proj_bbox = None
    proj_shape = None

    netcdf_with_time_dimension = False
    if collection is not None:
        # we found some collection level metadata
        item_assets = collection.extra_fields.get("item_assets", {})
        dimensions = set([tuple(v.get("dimensions")) for i in item_assets.values() if "cube:variables" in i for v in
                          i.get("cube:variables", {}).values()])
        # this is one way to determine if a time dimension is used, but it does depend on the use of item_assets and datacube extension.
        netcdf_with_time_dimension = len(dimensions) == 1 and "time" in dimensions.pop()

    for itm in intersecting_items:
        band_assets = {asset_id: asset for asset_id, asset
                       in dict(sorted(itm.get_assets().items())).items() if is_band_asset(asset)}

        builder = jvm.org.openeo.opensearch.OpenSearchResponses.featureBuilder()

        builder = (builder.withId(itm.id).withNominalDate(itm.properties.get("datetime") or itm.properties["start_datetime"]))


        for asset_id, asset in band_assets.items():
            asset_band_names = get_band_names(itm, asset)
            for asset_band_name in asset_band_names:
                if asset_band_name not in band_names:
                    band_names.append(asset_band_name)

            proj_epsg, proj_bbox, proj_shape = get_proj_metadata(itm, asset)

            builder = builder.addLink(asset.get_absolute_href() or asset.href, asset_id, asset_band_names)

        if proj_epsg:
            builder = builder.withCRS(f"EPSG:{proj_epsg}")
        if proj_bbox:
            builder = builder.withRasterExtent(*proj_bbox)

        if proj_bbox and proj_shape:
            cell_width, cell_height = _compute_cellsize(proj_bbox, proj_shape)
            builder = builder.withResolution(cell_width)

        latlon_bbox = BoundingBox.from_wsen_tuple(itm.bbox,4326) if itm.bbox else None
        item_bbox = latlon_bbox
        if proj_bbox is not None and proj_epsg is not None:
            item_bbox = BoundingBox.from_wsen_tuple(proj_bbox, crs=proj_epsg)
            latlon_bbox = item_bbox.reproject(4326)

        if latlon_bbox is not None:
            builder = builder.withBBox(latlon_bbox.as_wsen_tuple()[0], latlon_bbox.as_wsen_tuple()[1], latlon_bbox.as_wsen_tuple()[2], latlon_bbox.as_wsen_tuple()[3])

        f = builder.build()
        opensearch_client.addFeature(f)


        stac_bbox = (item_bbox if stac_bbox is None
                     else BoundingBox.from_wsen_tuple(item_bbox.as_polygon().union(stac_bbox.as_polygon()).bounds,
                                                      stac_bbox.crs))

        items_found = True

    if not items_found:
        raise no_data_available_exception

    if not band_names:
        raise OpenEOApiException(
            message=f'No band assets found in items; a band asset requires an "eo:bands" property with a "name".',
            status_code=400)

    target_bbox = requested_bbox or stac_bbox

    if not target_bbox:
        raise ProcessParameterInvalidException(
            process='load_stac',
            parameter='spatial_extent',
            reason=f'Unable to derive a spatial extent from provided STAC metadata: {url}, '
                   f'please provide a spatial extent.'
            )

    if proj_epsg and proj_bbox and proj_shape:  # exact resolution
        target_epsg = proj_epsg
        cell_width, cell_height = _compute_cellsize(proj_bbox, proj_shape)
    elif proj_epsg:  # about 10m in given CRS
        target_epsg = proj_epsg
        try:
            utm_zone_from_epsg(proj_epsg)
            cell_width = cell_height = 10.0
        except ValueError:
            target_bbox_center = target_bbox.as_polygon().centroid
            cell_width = cell_height = GeometryBufferer.transform_meter_to_crs(
                10.0, f"EPSG:{proj_epsg}", loi=(target_bbox_center.x, target_bbox_center.y))
    else:  # 10m UTM
        target_epsg = target_bbox.best_utm()
        cell_width = cell_height = 10.0

    metadata = GeopysparkCubeMetadata(metadata={}, dimensions=[
        # TODO: detect actual dimensions instead of this simple default?
        SpatialDimension(name="x", extent=[]), SpatialDimension(name="y", extent=[]),
        TemporalDimension(name='t', extent=[]),
        BandDimension(name="bands", bands=[Band(band_name) for band_name in band_names])
    ])

    if load_params.bands:
        metadata = metadata.filter_bands(load_params.bands)

    band_names = metadata.band_names

    if netcdf_with_time_dimension:
        pyramid_factory = jvm.org.openeo.geotrellis.layers.NetCDFCollection
    else:
        pyramid_factory = jvm.org.openeo.geotrellis.file.PyramidFactory(
            opensearch_client,
            url,  # openSearchCollectionId, not important
            band_names,  # openSearchLinkTitles
            None,  # rootPath, not important
            jvm.geotrellis.raster.CellSize(cell_width, cell_height),
            False  # experimental
        )

    extent = jvm.geotrellis.vector.Extent(*map(float, target_bbox.as_wsen_tuple()))
    extent_crs = target_bbox.crs

    geometries = load_params.aggregate_spatial_geometries

    if not geometries:
        projected_polygons = jvm.org.openeo.geotrellis.ProjectedPolygons.fromExtent(extent, extent_crs)
    else:
        projected_polygons = to_projected_polygons(
            jvm, geometries, crs=extent_crs, buffer_points=True
        )

    projected_polygons = getattr(
        getattr(jvm.org.openeo.geotrellis, "ProjectedPolygons$"), "MODULE$"
    ).reproject(projected_polygons, target_epsg)

    metadata_properties = {}
    correlation_id = env.get('correlation_id', '')

    data_cube_parameters = jvm.org.openeo.geotrelliscommon.DataCubeParameters()
    getattr(data_cube_parameters, "layoutScheme_$eq")("FloatingLayoutScheme")

    feature_flags = load_params.get("featureflags", {})
    tilesize = feature_flags.get("tilesize", None)
    if tilesize:
        getattr(data_cube_parameters, "tileSize_$eq")(tilesize)
    single_level = env.get('pyramid_levels', 'all') != 'all'

    if netcdf_with_time_dimension:
        pyramid = pyramid_factory.datacube_seq(projected_polygons, from_date.isoformat(), to_date.isoformat(),
                                               metadata_properties, correlation_id, data_cube_parameters,
                                               opensearch_client)
    elif single_level:
        pyramid = pyramid_factory.datacube_seq(projected_polygons, from_date.isoformat(), to_date.isoformat(),
                                               metadata_properties, correlation_id, data_cube_parameters)
    else:
        if requested_bbox:
            extent = jvm.geotrellis.vector.Extent(*map(float, requested_bbox.as_wsen_tuple()))
            extent_crs = requested_bbox.crs
        else:
            extent = jvm.geotrellis.vector.Extent(-180.0, -90.0, 180.0, 90.0)
            extent_crs = "EPSG:4326"

        pyramid = pyramid_factory.pyramid_seq(
            extent, extent_crs, from_date.isoformat(), to_date.isoformat(),
            metadata_properties, correlation_id
        )

    metadata = metadata.filter_temporal(from_date.isoformat(), to_date.isoformat())

    metadata = metadata.filter_bbox(
        west=extent.xmin(),
        south=extent.ymin(),
        east=extent.xmax(),
        north=extent.ymax(),
        crs=extent_crs,
    )

    temporal_tiled_raster_layer = jvm.geopyspark.geotrellis.TemporalTiledRasterLayer
    option = jvm.scala.Option

    # noinspection PyProtectedMember
    levels = {pyramid.apply(index)._1(): TiledRasterLayer(LayerType.SPACETIME, temporal_tiled_raster_layer(
        option.apply(pyramid.apply(index)._1()), pyramid.apply(index)._2())) for index in
              range(0, pyramid.size())}

    return GeopysparkDataCube(pyramid=gps.Pyramid(levels), metadata=metadata)


def _compute_cellsize(proj_bbox, proj_shape):
    xmin, ymin, xmax, ymax = proj_bbox
    rows, cols = proj_shape
    cell_width = (xmax - xmin) / cols
    cell_height = (ymax - ymin) / rows
    return cell_width, cell_height


def extract_own_job_info(url: str, user_id: str, batch_jobs: backend.BatchJobs) -> Optional[BatchJobMetadata]:
    path_segments = urlparse(url).path.split('/')

    if len(path_segments) < 3:
        return None

    jobs_position_segment, job_id, results_position_segment = path_segments[-3:]
    if jobs_position_segment != "jobs" or results_position_segment != "results":
        return None

    try:
        return batch_jobs.get_job_info(job_id=job_id, user_id=user_id)
    except JobNotFoundException:
        return None
