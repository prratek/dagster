import logging
import pickle

from dagster import Field, ObjectManager, StringSource, check, object_manager
from dagster.utils import PICKLE_PROTOCOL
from dagster_azure.adls2.utils import ResourceNotFoundError

_LEASE_DURATION = 60  # One minute


class PickledObjectADLS2ObjectManager(ObjectManager):
    def __init__(self, file_system, adls2_client, blob_client, prefix="dagster"):
        self.adls2_client = adls2_client
        self.file_system_client = self.adls2_client.get_file_system_client(file_system)
        # We also need a blob client to handle copying as ADLS doesn't have a copy API yet
        self.blob_client = blob_client
        self.blob_container_client = self.blob_client.get_container_client(file_system)
        self.prefix = check.str_param(prefix, "prefix")

        self.lease_duration = _LEASE_DURATION
        self.file_system_client.get_file_system_properties()

    def _get_path(self, context):
        run_id, step_key, output_name = context.get_run_scoped_output_identifier()
        return "/".join([self.prefix, "storage", run_id, "files", step_key, output_name,])

    def _rm_object(self, key):
        check.str_param(key, "key")
        check.param_invariant(len(key) > 0, "key")

        # This operates recursively already so is nice and simple.
        self.file_system_client.delete_file(key)

    def _has_object(self, key):
        check.str_param(key, "key")
        check.param_invariant(len(key) > 0, "key")

        try:
            file = self.file_system_client.get_file_client(key)
            file.get_file_properties()
            return True
        except ResourceNotFoundError:
            return False

    def _uri_for_key(self, key, protocol=None):
        check.str_param(key, "key")
        protocol = check.opt_str_param(protocol, "protocol", default="abfss://")
        return "{protocol}{filesystem}@{account}.dfs.core.windows.net/{key}".format(
            protocol=protocol,
            filesystem=self.file_system_client.file_system_name,
            account=self.file_system_client.account_name,
            key=key,
        )

    def load_input(self, context):
        key = self._get_path(context.upstream_output)
        file = self.file_system_client.get_file_client(key)
        stream = file.download_file()
        obj = pickle.loads(stream.readall())

        return obj

    def handle_output(self, context, obj):
        key = self._get_path(context)
        logging.info("Writing ADLS2 object at: " + self._uri_for_key(key))

        if self._has_object(key):
            logging.warning("Removing existing ADLS2 key: {key}".format(key=key))
            self._rm_object(key)

        pickled_obj = pickle.dumps(obj, PICKLE_PROTOCOL)

        file = self.file_system_client.create_file(key)
        with file.acquire_lease(self.lease_duration) as lease:
            file.upload_data(pickled_obj, lease=lease, overwrite=True)


@object_manager(
    config_schema={
        "adls2_file_system": Field(StringSource, description="ADLS Gen2 file system name"),
        "adls2_prefix": Field(StringSource, is_required=False, default_value="dagster"),
    },
    required_resource_keys={"adls2"},
)
def adls2_object_manager(init_context):
    """Persistent object manager using Azure Data Lake Storage Gen2 for storage.

    Suitable for objects storage for distributed executors, so long as
    each execution node has network connectivity and credentials for ADLS and
    the backing container.

    Attach this resource definition to a :py:class:`~dagster.ModeDefinition`
    in order to make it available to your pipeline:

    .. code-block:: python

        pipeline_def = PipelineDefinition(
            mode_defs=[
                ModeDefinition(
                    resource_defs={
                        'object_manager': adls2_object_manager,
                        'adls2': adls2_resource, ...},
                ), ...
            ], ...
        )

    You may configure this storage as follows:

    .. code-block:: YAML

        resources:
            object_manager:
                config:
                    adls2_file_system: my-cool-file-system
                    adls2_prefix: good/prefix-for-files-
    """
    adls_resource = init_context.resources.adls2
    adls2_client = adls_resource.adls2_client
    blob_client = adls_resource.blob_client
    pickled_object_manager = PickledObjectADLS2ObjectManager(
        init_context.resource_config["adls2_file_system"],
        adls2_client,
        blob_client,
        init_context.resource_config.get("adls2_prefix"),
    )
    return pickled_object_manager
