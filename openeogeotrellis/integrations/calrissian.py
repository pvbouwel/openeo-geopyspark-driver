import logging
import dataclasses
import time
from typing import Optional

from openeo.util import ContextTimer
from openeo_driver.utils import generate_unique_id
from openeogeotrellis.config import get_backend_config

import kubernetes.client

_log = logging.getLogger(__name__)

@dataclasses.dataclass(frozen=True)
class VolumeInfo:
    name: str
    claim_name: str
    mount_path: str
    read_only: Optional[bool] = None


class CalrissianJobLauncher:
    """
    Helper class to launch a Calrissian job on Kubernetes.
    """

    def __init__(
        self,
        *,
        namespace: Optional[str] = None,
        name_base: Optional[str] = None,
        backoff_limit: int = 1,
    ):
        self._namespace = namespace or get_backend_config().calrissian_namespace
        assert self._namespace
        self._name_base = name_base
        self._backoff_limit = backoff_limit

        # TODO: config for this?
        self._security_context = kubernetes.client.V1SecurityContext(run_as_user=1000, run_as_group=1000)

    def _get_name(self, affix: str) -> str:
        return f"{self._name_base}-{affix}" if self._name_base else generate_unique_id(prefix=affix)

    def create_input_staging_job_manifest(self) -> kubernetes.client.V1Job:
        """
        Create a k8s manifest for a Calrissian input staging job.
        """
        name = self._get_name("cal-input")
        _log.info("Creating input staging job manifest: {name=}")

        volumes = [
            # TODO: build these volume infos in init?
            VolumeInfo(
                name="calrissian-input-data",
                claim_name="calrissian-input-data",
                mount_path="/calrissian/input-data",
                # TODO: note: no read_only here. Instead do input staging as part of deployment, instead of on the fly?
            ),
        ]

        container = kubernetes.client.V1Container(
            name="calrissian-input-staging",
            image="alpine:3",
            security_context=self._security_context,
            command=["/bin/sh"],
            args=[
                "-c",
                "; ".join(
                    [
                        "set -euxo pipefail",
                        # TODO: better way to deploy and fetch these resources?
                        "wget -O /tmp/calrissian-resources.tar.gz https://artifactory.vgt.vito.be/artifactory/auxdata-public/openeo/calrissian-resources/calrissian-resources.tar.gz",
                        "tar -xzvf /tmp/calrissian-resources.tar.gz -C /calrissian/input-data",
                        "ls -al /calrissian/input-data",
                    ]
                ),
            ],
            volume_mounts=[
                kubernetes.client.V1VolumeMount(name=v.name, mount_path=v.mount_path, read_only=v.read_only)
                for v in volumes
            ],
        )
        manifest = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=name,
                namespace=self._namespace,
            ),
            spec=kubernetes.client.V1JobSpec(
                template=kubernetes.client.V1PodTemplateSpec(
                    spec=kubernetes.client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",
                        volumes=[
                            kubernetes.client.V1Volume(
                                name=v.name,
                                persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=v.claim_name,
                                    read_only=v.read_only,
                                ),
                            )
                            for v in volumes
                        ],
                    )
                ),
                backoff_limit=self._backoff_limit,
            ),
        )
        return manifest

    def create_cwl_job_manifest(
        self,
        # TODO: arguments to set an actual CWL workflow and inputs
    ) -> kubernetes.client.V1Job:
        name = self._get_name("cal-cwl")
        _log.info(f"Creating CWL job manifest: {name=}")

        container_image = get_backend_config().calrissian_image
        assert container_image

        volumes = [
            # TODO: build these volume infos in init?
            VolumeInfo(
                name="calrissian-input-data",
                claim_name="calrissian-input-data",
                mount_path="/calrissian/input-data",
                read_only=True,
            ),
            VolumeInfo(
                name="calrissian-tmpout",
                claim_name="calrissian-tmpout",
                mount_path="/calrissian/tmpout",
            ),
            VolumeInfo(
                name="calrissian-output-data",
                claim_name="calrissian-output-data",
                mount_path="/calrissian/output-data",
            ),
        ]

        calrissian_arguments = [
            "--max-ram",
            "2G",
            "--max-cores",
            "1",
            "--debug",
            "--tmp-outdir-prefix",
            "/calrissian/tmpout/",
            "--outdir",
            "/calrissian/output-data/",
            "/calrissian/input-data/hello-world.cwl",
            "--message",
            "Hello EO world!",
        ]

        container = kubernetes.client.V1Container(
            name="calrissian-job",
            image=container_image,
            security_context=self._security_context,
            command=["calrissian"],
            args=calrissian_arguments,
            volume_mounts=[
                kubernetes.client.V1VolumeMount(name=v.name, mount_path=v.mount_path, read_only=v.read_only)
                for v in volumes
            ],
            env=[
                kubernetes.client.V1EnvVar(
                    name="CALRISSIAN_POD_NAME",
                    value_from=kubernetes.client.V1EnvVarSource(
                        field_ref=kubernetes.client.V1ObjectFieldSelector(field_path="metadata.name")
                    ),
                )
            ],
        )
        manifest = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=name,
                namespace=self._namespace,
            ),
            spec=kubernetes.client.V1JobSpec(
                template=kubernetes.client.V1PodTemplateSpec(
                    spec=kubernetes.client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",
                        volumes=[
                            kubernetes.client.V1Volume(
                                name=v.name,
                                persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=v.claim_name,
                                    read_only=v.read_only,
                                ),
                            )
                            for v in volumes
                        ],
                    )
                ),
                backoff_limit=self._backoff_limit,
            ),
        )
        return manifest

    def launch_job_and_wait(
        self,
        manifest: kubernetes.client.V1Job,
        *,
        sleep: float = 5,
        timeout: float = 60,
    ) -> kubernetes.client.V1Job:
        """Launch a k8s job and wait (with active polling) for it to finish."""

        k8s_batch = kubernetes.client.BatchV1Api()

        # Launch job.
        job: kubernetes.client.V1Job = k8s_batch.create_namespaced_job(
            namespace=self._namespace,
            body=manifest,
        )
        job_name = job.metadata.name
        _log.info(
            f"Created CWL job {job.metadata.name=} {job.metadata.namespace=} {job.metadata.creation_timestamp=} {job.metadata.uid=}"
        )

        # Track job status (active polling).
        final_status = None
        with ContextTimer() as timer:
            while timer.elapsed() < timeout:
                job: kubernetes.client.V1Job = k8s_batch.read_namespaced_job(name=job_name, namespace=self._namespace)
                _log.info(f"CWL job {job_name=} {timer.elapsed()=:.2f} {job.status=}")
                if job.status.conditions:
                    if any(c.type == "Failed" and c.status == "True" for c in job.status.conditions):
                        final_status = "failed"
                        break
                    elif any(c.type == "Complete" and c.status == "True" for c in job.status.conditions):
                        final_status = "complete"
                        break
                time.sleep(sleep)

        _log.info(f"CWL job {job_name=} {timer.elapsed()=:.2f} {final_status=}")
        if final_status == "complete":
            pass
        elif final_status is None:
            raise TimeoutError(f"CWL Job {job_name} did not finish within {timeout}s")
        elif final_status != "complete":
            raise RuntimeError(f"CWL Job {job_name} failed with {final_status=} after {timer.elapsed()=:.2f}s")
        else:
            raise ValueError("CWL")

        # TODO: how to resolve and extract the results?

        return job
