import json
import logging
import os
import shutil
import stat
from copy import deepcopy
from pathlib import Path
from traceback_with_variables import Format, format_exc
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import sys
from itertools import chain
from openeo.util import ensure_dir, TimingLogger, dict_no_none
from py4j.protocol import Py4JError, Py4JJavaError
from pyspark import SparkContext, SparkConf
from pyspark.profiler import BasicProfiler
from shapely.geometry import mapping

from openeo_driver import ProcessGraphDeserializer
from openeo_driver.datacube import DriverDataCube, DriverVectorCube
from openeo_driver.delayed_vector import DelayedVector
from openeo_driver.dry_run import DryRunDataTracer
from openeo_driver.save_result import (ImageCollectionResult, JSONResult, SaveResult, MlModelResult, VectorCubeResult, )
from openeo_driver.users import User
from openeo_driver.util.logging import (GlobalExtraLoggingFilter, get_logging_config, setup_logging,
                                        LOGGING_CONTEXT_BATCH_JOB, LOG_HANDLER_STDERR_JSON, LOG_HANDLER_FILE_JSON, )
from openeo_driver.utils import EvalEnv
from openeo_driver.workspacerepository import backend_config_workspace_repository
from openeogeotrellis._version import __version__
from openeogeotrellis.backend import JOB_METADATA_FILENAME, GeoPySparkBackendImplementation
from openeogeotrellis.collect_unique_process_ids_visitor import CollectUniqueProcessIdsVisitor
from openeogeotrellis.config import get_backend_config
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.deploy import load_custom_processes
from openeogeotrellis.deploy.batch_job_metadata import _assemble_result_metadata, _transform_stac_metadata, \
    _convert_job_metadatafile_outputs_to_s3_urls, _get_tracker_metadata
from openeogeotrellis.integrations.hadoop import setup_kerberos_auth
from openeogeotrellis.udf import (collect_python_udf_dependencies, install_python_udf_dependencies,
                                  UDF_PYTHON_DEPENDENCIES_FOLDER_NAME, )
from openeogeotrellis.utils import (describe_path, log_memory, get_jvm, add_permissions, json_default, )

logger = logging.getLogger('openeogeotrellis.deploy.batch_job')


OPENEO_LOGGING_THRESHOLD = os.environ.get("OPENEO_LOGGING_THRESHOLD", "INFO")
OPENEO_BATCH_JOB_ID = os.environ.get("OPENEO_BATCH_JOB_ID", "unknown-job")
GlobalExtraLoggingFilter.set("job_id", OPENEO_BATCH_JOB_ID)


def _create_job_dir(job_dir: Path):
    logger.info("creating job dir {j!r} (parent dir: {p}))".format(j=job_dir, p=describe_path(job_dir.parent)))
    ensure_dir(job_dir)
    if not ConfigParams().is_kube_deploy:
        try:
            shutil.chown(job_dir, user=None, group='eodata')
        except LookupError as e:
            logger.warning(f"Could not change group of {job_dir} to eodata.")
        except PermissionError as e:
            logger.warning(f"Could not change group of {job_dir} to eodata, no permissions.")

    if not get_backend_config().fuse_mount_batchjob_s3_bucket:
        add_permissions(job_dir, stat.S_ISGID | stat.S_IWGRP)  # make children inherit this group


def _parse(job_specification_file: str) -> Dict:
    with open(job_specification_file, 'rt', encoding='utf-8') as f:
        job_specification = json.load(f)

    return job_specification


def _deserialize_dependencies(arg: str) -> List[dict]:
    return json.loads(arg)


def _get_sentinel_hub_credentials_from_spark_conf(conf: SparkConf) -> Optional[Tuple[str, str]]:
    default_client_id = conf.get('spark.openeo.sentinelhub.client.id.default')
    default_client_secret = conf.get('spark.openeo.sentinelhub.client.secret.default')

    return (default_client_id, default_client_secret) if default_client_id and default_client_secret else None


def _get_vault_token(conf: SparkConf) -> Optional[str]:
    return conf.get('spark.openeo.vault.token')


def _get_access_token(conf: SparkConf) -> Optional[str]:
    return conf.get('spark.openeo.access_token')


def main(argv: List[str]) -> None:
    logger.debug(f"batch_job.py argv: {argv}")
    logger.debug(f"batch_job.py {os.getpid()=} {os.getppid()=} {os.getcwd()=}")
    logger.debug(f"batch_job.py version info {get_backend_config().capabilities_deploy_metadata}")

    if len(argv) < 9:
        raise Exception(
            f"usage: {argv[0]} "
            "<job specification input file> <job directory> <results output file name> "
            "<metadata file name> <api version> <dependencies> <user id> <max soft errors ratio> "
            "[Sentinel Hub client alias]"
        )

    job_specification_file = argv[1]
    job_dir = Path(argv[2])
    output_file = job_dir / argv[3]
    metadata_file = job_dir / argv[4]
    api_version = argv[5]
    dependencies = _deserialize_dependencies(argv[6])
    user_id = argv[7]
    GlobalExtraLoggingFilter.set("user_id", user_id)
    max_soft_errors_ratio = float(argv[8])
    sentinel_hub_client_alias = argv[9] if len(argv) >= 10 else None

    _create_job_dir(job_dir)

    # Override default temp dir (under CWD). Original default temp dir `/tmp` might be cleaned up unexpectedly.
    temp_dir = Path(os.getcwd()) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Using temp dir {t}".format(t=temp_dir))
    os.environ["TMPDIR"] = str(temp_dir)

    if ConfigParams().is_kube_deploy:
        if not get_backend_config().fuse_mount_batchjob_s3_bucket:
            from openeogeotrellis.utils import s3_client

            bucket = os.environ.get('SWIFT_BUCKET')
            s3_instance = s3_client()

            s3_instance.download_file(bucket, job_specification_file.strip("/"), job_specification_file )

    job_specification = _parse(job_specification_file)
    load_custom_processes()

    conf = (SparkConf()
            .set("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
            .set(key='spark.kryo.registrator', value='geotrellis.spark.store.kryo.KryoRegistrator')
            .set("spark.kryo.classesToRegister", "org.openeo.geotrellisaccumulo.SerializableConfiguration,ar.com.hjg.pngj.ImageInfo,ar.com.hjg.pngj.ImageLineInt,geotrellis.raster.RasterRegion$GridBoundsRasterRegion"))

    def context_with_retry(conf):
        retry_counter = 0
        while retry_counter < 5:
            retry_counter += 1
            try:
                return SparkContext(conf=conf)
            except Py4JJavaError as e:
                if retry_counter == 5:
                    raise
                else:
                    logger.info(f"Failed to create SparkContext, retrying {retry_counter} ... {repr(GeoPySparkBackendImplementation.summarize_exception_static(e))}")

    with context_with_retry(conf) as sc:
        principal = sc.getConf().get("spark.yarn.principal")
        key_tab = sc.getConf().get("spark.yarn.keytab")

        default_sentinel_hub_credentials = _get_sentinel_hub_credentials_from_spark_conf(sc.getConf())
        vault_token = _get_vault_token(sc.getConf())
        access_token = _get_access_token(sc.getConf())

        if get_backend_config().setup_kerberos_auth:
            setup_kerberos_auth(principal, key_tab)

        def run_driver():
            run_job(
                job_specification=job_specification, output_file=output_file, metadata_file=metadata_file,
                api_version=api_version, job_dir=job_dir, dependencies=dependencies, user_id=user_id,
                max_soft_errors_ratio=max_soft_errors_ratio,
                default_sentinel_hub_credentials=default_sentinel_hub_credentials,
                sentinel_hub_client_alias=sentinel_hub_client_alias, vault_token=vault_token, access_token=access_token,
            )

        if sc.getConf().get('spark.python.profile', 'false').lower() == 'true':
            # Including the driver in the profiling: a bit hacky solution but spark profiler api does not allow passing args&kwargs
            driver_profile = BasicProfiler(sc)
            driver_profile.profile(run_driver)
            # running the driver code and adding driver's profiling results as "RDD==-1"
            sc.profiler_collector.add_profiler(-1, driver_profile)
            # collect profiles into a zip file
            profile_dumps_dir = job_dir / 'profile_dumps'
            sc.dump_profiles(profile_dumps_dir)

            profile_zip = shutil.make_archive(base_name=str(profile_dumps_dir), format='gztar',
                                              root_dir=profile_dumps_dir)
            add_permissions(Path(profile_zip), stat.S_IWGRP)

            shutil.rmtree(profile_dumps_dir,
                          onerror=lambda func, path, exc_info:
                          logger.warning(f"could not recursively delete {profile_dumps_dir}: {func} {path} failed",
                                         exc_info=exc_info))

            logger.info("Saved profiling info to: " + profile_zip)
        else:
            run_driver()


@log_memory
def run_job(
    job_specification,
    output_file: Union[str, Path],
    metadata_file: Union[str, Path],
    api_version: str,
    job_dir: Union[str, Path],
    dependencies: List[dict],
    user_id: str = None,
    max_soft_errors_ratio: float = 0.0,
    default_sentinel_hub_credentials=None,
    sentinel_hub_client_alias="default",
    vault_token: str = None,
    access_token: str = None,
):
    result_metadata = {}

    try:
        # We actually expect type Path, but in reality paths as strings tend to
        # slip in anyway, so we better catch them and convert them.
        output_file = Path(output_file)
        metadata_file = Path(metadata_file)
        job_dir = Path(job_dir)

        logger.info(f"Job spec: {json.dumps(job_specification,indent=1)}")
        logger.info(f"{job_dir=}, {job_dir.resolve()=}, {output_file=}, {metadata_file=}")
        process_graph = job_specification['process_graph']
        job_options = job_specification.get("job_options", {})

        try:
            _extract_and_install_udf_dependencies(process_graph=process_graph, job_dir=job_dir)
        except Exception as e:
            logger.exception(f"Failed extracting and installing UDF dependencies: {e}")

        backend_implementation = GeoPySparkBackendImplementation(
            use_job_registry=bool(get_backend_config().ejr_api),
        )

        if default_sentinel_hub_credentials is not None:
            backend_implementation.set_default_sentinel_hub_credentials(*default_sentinel_hub_credentials)

        logger.info(f"Using backend implementation {backend_implementation}")
        correlation_id = OPENEO_BATCH_JOB_ID
        logger.info(f"Correlation id: {correlation_id}")
        env_values = {
            'version': api_version or "1.0.0",
            'pyramid_levels': 'highest',
            'user': User(user_id=user_id,
                         internal_auth_data=dict_no_none(access_token=access_token)),
            'require_bounds': True,
            'correlation_id': correlation_id,
            'dependencies': dependencies.copy(),  # will be mutated (popped) during evaluation
            'backend_implementation': backend_implementation,
            'max_soft_errors_ratio': max_soft_errors_ratio,
            'sentinel_hub_client_alias': sentinel_hub_client_alias,
            'vault_token': vault_token
        }
        job_option_whitelist = [
            "data_mask_optimization",
            "node_caching",
            "allow_empty_cubes",
        ]
        env_values.update({k: job_options[k] for k in job_option_whitelist if k in job_options})
        env = EvalEnv(env_values)
        tracer = DryRunDataTracer()
        logger.info("Starting process graph evaluation")
        pg_copy = deepcopy(process_graph)
        result = ProcessGraphDeserializer.evaluate(process_graph, env=env, do_dry_run=tracer)
        logger.info("Evaluated process graph, result (type {t}): {r!r}".format(t=type(result), r=result))

        if isinstance(result, DelayedVector):
            geojsons = (mapping(geometry) for geometry in result.geometries_wgs84)
            result = JSONResult(geojsons)

        if isinstance(result, DriverDataCube):
            format_options = job_specification.get('output', {})
            format_options["batch_mode"] = True
            result = ImageCollectionResult(cube=result, format='GTiff', options=format_options)

        if isinstance(result, DriverVectorCube):
            format_options = job_specification.get('output', {})
            format_options["batch_mode"] = True
            result = VectorCubeResult(cube=result, format='GTiff', options=format_options)

        results = result if isinstance(result, List) else [result]
        results = [result if isinstance(result, SaveResult) else JSONResult(result) for result in results]

        global_metadata_attributes = {
            "title": job_specification.get("title", ""),
            "description": job_specification.get("description", ""),
            "institution": "openEO platform - Geotrellis backend: " + __version__
        }

        assets_metadata = {}
        ml_model_metadata = None

        for result in results:
            result.options["batch_mode"] = True
            result.options["file_metadata"] = global_metadata_attributes
            if result.options.get("sample_by_feature"):
                geoms = tracer.get_last_geometry("filter_spatial")
                if geoms is None:
                    logger.warning("sample_by_feature enabled, but no geometries found. "
                                   "They can be specified using filter_spatial.")
                else:
                    result.options["geometries"] = geoms
                if result.options["geometries"] is None:
                    logger.error("samply_by_feature was set, but no geometries provided through filter_spatial. "
                                 "Make sure to provide geometries.")
            the_assets_metadata = result.write_assets(str(output_file))
            if isinstance(result, MlModelResult):
                ml_model_metadata = result.get_model_metadata(str(output_file))
                logger.info("Extracted ml model metadata from %s" % output_file)
            for name, asset in the_assets_metadata.items():
                add_permissions(Path(asset["href"]), stat.S_IWGRP)
            logger.info(f"wrote {len(the_assets_metadata)} assets to {output_file}")
            assets_metadata = {**assets_metadata, **the_assets_metadata}

        if any(dependency['card4l'] for dependency in dependencies):  # TODO: clean this up
            logger.debug("awaiting Sentinel Hub CARD4L data...")

            s3_service = get_jvm().org.openeo.geotrellissentinelhub.S3Service()

            poll_interval_secs = 10
            max_delay_secs = 600

            card4l_source_locations = [dependency['source_location'] for dependency in dependencies if dependency['card4l']]

            for source_location in set(card4l_source_locations):
                uri_parts = urlparse(source_location)
                bucket_name = uri_parts.hostname
                request_group_id = uri_parts.path[1:]

                try:
                    # TODO: incorporate index to make sure the files don't clash
                    s3_service.download_stac_data(bucket_name, request_group_id, str(job_dir), poll_interval_secs,
                                                  max_delay_secs)
                    logger.info("downloaded CARD4L data in {b}/{g} to {d}"
                                .format(b=bucket_name, g=request_group_id, d=job_dir))
                except Py4JJavaError as e:
                    java_exception = e.java_exception

                    if (java_exception.getClass().getName() ==
                            'org.openeo.geotrellissentinelhub.S3Service$StacMetadataUnavailableException'):
                        logger.warning("could not find CARD4L metadata to download from s3://{b}/{r} after {d}s"
                                       .format(b=bucket_name, r=request_group_id, d=max_delay_secs))
                    else:
                        raise e

            _transform_stac_metadata(job_dir)

        unique_process_ids = CollectUniqueProcessIdsVisitor().accept_process_graph(process_graph).process_ids

        # this is subtle: result now points to the last of possibly several results (#295); it corresponds to
        # the terminal save_result node of the process graph
        if "file_metadata" in result.options:
            result.options["file_metadata"]["providers"] = [
                {
                    "name": "VITO",
                    "description": "This data was processed on an openEO backend maintained by VITO.",
                    "roles": [
                        "processor"
                    ],
                    "processing:facility": "openEO Geotrellis backend",
                    "processing:software": {
                        "Geotrellis backend": __version__
                    },
                    "processing:expression": {
                        "format": "openeo",
                        "expression": pg_copy
                    }
                }]

        result_metadata = _assemble_result_metadata(tracer=tracer, result=result, output_file=output_file,
                                                    unique_process_ids=unique_process_ids,
                                                    asset_metadata=assets_metadata,
                                                    ml_model_metadata=ml_model_metadata,skip_gdal=True)

        write_metadata({**result_metadata, **_get_tracker_metadata("")}, metadata_file, job_dir)
        logger.info("Starting GDAL-based retrieval of asset metadata")
        result_metadata = _assemble_result_metadata(tracer=tracer, result=result, output_file=output_file,
                                                    unique_process_ids=unique_process_ids,
                                                    asset_metadata=assets_metadata,
                                                    ml_model_metadata=ml_model_metadata,skip_gdal=False)

        _export_workspace(result, result_metadata, stac_metadata_dir=job_dir)
    finally:

        write_metadata({**result_metadata, **_get_tracker_metadata("")}, metadata_file, job_dir)

        try:
            get_jvm().com.azavea.gdal.GDALWarp.deinit()
        except Py4JError as e:
            if str(e) == "com.azavea.gdal.GDALWarp does not exist in the JVM":
                logger.debug(f"intentionally swallowing exception {e}", exc_info=True)
            else:
                raise


def write_metadata(metadata, metadata_file, job_dir):
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, default=json_default)
    add_permissions(metadata_file, stat.S_IWGRP)
    logger.info("wrote metadata to %s" % metadata_file)
    if ConfigParams().is_kube_deploy:
        if not get_backend_config().fuse_mount_batchjob_s3_bucket:
            from openeogeotrellis.utils import s3_client

            _convert_job_metadatafile_outputs_to_s3_urls(metadata_file)

            bucket = os.environ.get('SWIFT_BUCKET')
            s3_instance = s3_client()

            logger.info("Writing results to object storage")
            for file in os.listdir(job_dir):
                full_path = str(job_dir) + "/" + file
                s3_instance.upload_file(full_path, bucket, full_path.strip("/"))
        else:
            _convert_job_metadatafile_outputs_to_s3_urls(metadata_file)


def _export_workspace(result: SaveResult, result_metadata: dict, stac_metadata_dir: Path):
    asset_paths = [Path(asset["href"]) for asset in result_metadata.get("assets", {}).values()]
    stac_paths = _write_exported_stac_collection(stac_metadata_dir, result_metadata)
    result.export_workspace(workspace_repository=backend_config_workspace_repository,
                            files=asset_paths + stac_paths,
                            default_merge=OPENEO_BATCH_JOB_ID)


def _write_exported_stac_collection(job_dir: Path, result_metadata: dict) -> List[Path]:  # TODO: change to Set?
    def write_stac_item_file(asset_id: str, asset: dict) -> Path:
        item_file = job_dir / f"{asset_id}.json"

        stac_item = {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": asset_id,
            "geometry": asset.get("geometry"),
            "bbox": asset.get("bbox"),
            "properties": {
                "datetime": asset.get("datetime"),
            },
            "links": [],  # TODO
            "assets": {
                asset_id: dict_no_none(**{
                    "href": f"./{Path(asset['href']).name}",
                    "roles": asset.get("roles"),
                    "type": asset.get("type"),
                    "eo:bands": asset.get("bands"),
                })
            },
        }

        with open(item_file, "wt") as fi:
            json.dump(stac_item, fi)

        return item_file

    item_files = [write_stac_item_file(asset_id, asset)
                  for asset_id, asset in result_metadata.get("assets", {}).items()]

    def item_link(item_file: Path) -> dict:
        return {
            "href": f"./{item_file.name}",
            "rel": "item",
            "type": "application/geo+json",
        }

    stac_collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": OPENEO_BATCH_JOB_ID,
        "description": "TODO",  # TODO
        "license": "TODO",  # TODO
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},  # TODO
            "temporal": {"interval": [[None, None]]}  # TODO
        },
        "links": [item_link(item_file) for item_file in item_files],
    }

    collection_file = job_dir / "collection.json"
    with open(collection_file, "wt") as fc:
        json.dump(stac_collection, fc)

    return item_files + [collection_file]


def _extract_and_install_udf_dependencies(process_graph: dict, job_dir: Path):
    udf_dep_map = collect_python_udf_dependencies(process_graph)
    logger.debug(f"Extracted {udf_dep_map=}")
    if len(udf_dep_map) > 1:
        logger.warning("Merging dependencies from multiple UDF runtimes/versions")
    udf_deps = set(d for ds in udf_dep_map.values() for d in ds)
    if udf_deps:
        # TODO: add "udf_deps" folder to python path where appropriate
        install_python_udf_dependencies(
            dependencies=udf_deps, target=job_dir / UDF_PYTHON_DEPENDENCIES_FOLDER_NAME, timeout=20
        )


def start_main():
    setup_logging(
        get_logging_config(
            root_handlers=[LOG_HANDLER_STDERR_JSON if ConfigParams().is_kube_deploy else LOG_HANDLER_FILE_JSON],
            context=LOGGING_CONTEXT_BATCH_JOB,
            root_level=OPENEO_LOGGING_THRESHOLD,
        ),
        capture_unhandled_exceptions=False,  # not needed anymore, as we have a try catch around everything
    )

    try:
        with TimingLogger(f"batch_job.py main {os.getpid()=}", logger=logger):
            main(sys.argv)
    except Exception as e:
        error_summary = GeoPySparkBackendImplementation.summarize_exception_static(e)
        most_recent_exception = sys.exc_info()[1]
        fmt = Format(max_value_str_len=1000)
        logger.exception("OpenEO batch job failed: " + error_summary.summary)
        logger.error(
            "Batch job error stack trace with locals", 
            extra={"exc_info_with_locals": format_exc(e, fmt = fmt)}
        )
        raise

if __name__ == "__main__":
    start_main()