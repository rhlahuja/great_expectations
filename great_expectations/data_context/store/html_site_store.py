import logging
import os
import inspect
from mimetypes import guess_type

from great_expectations.data_context.types.resource_identifiers import (
    ExpectationSuiteIdentifier,
    ValidationResultIdentifier,
    SiteSectionIdentifier,
)
from great_expectations.data_context.util import (
    load_class,
    instantiate_class_from_config,
    file_relative_path
)
from great_expectations.exceptions import (
    DataContextError,
    ClassInstantiationError,
)
from great_expectations.util import verify_dynamic_loading_support
from .tuple_store_backend import TupleStoreBackend
from ...core.data_context_key import DataContextKey

logger = logging.getLogger(__name__)


class HtmlSiteStore(object):
    _key_class = SiteSectionIdentifier

    def __init__(self, store_backend=None, runtime_environment=None):
        store_backend_module_name = store_backend.get("module_name", "great_expectations.data_context.store")
        store_backend_class_name = store_backend.get("class_name", "TupleFilesystemStoreBackend")
        verify_dynamic_loading_support(module_name=store_backend_module_name)
        store_class = load_class(store_backend_class_name, store_backend_module_name)

        # Store Class was loaded successfully; verify that it is of a correct subclass.
        if not issubclass(store_class, TupleStoreBackend):
            raise DataContextError("Invalid configuration: HtmlSiteStore needs a TupleStoreBackend")
        if "filepath_template" in store_backend or ("fixed_length_key" in store_backend and
                                                    store_backend["fixed_length_key"] is True):
            logger.warning("Configuring a filepath_template or using fixed_length_key is not supported in SiteBuilder: "
                           "filepaths will be selected based on the type of asset rendered.")

        # One thing to watch for is reversibility of keys.
        # If several types are being written to overlapping directories, we could get collisions.
        module_name = 'great_expectations.data_context.store'
        filepath_prefix = 'expectations'
        filepath_suffix = '.html'
        expectation_suite_identifier_obj = instantiate_class_from_config(
            config=store_backend,
            runtime_environment=runtime_environment,
            config_defaults={
                "module_name": module_name,
                "filepath_prefix": filepath_prefix,
                "filepath_suffix": filepath_suffix,
            }
        )
        if not expectation_suite_identifier_obj:
            raise ClassInstantiationError(
                module_name=module_name,
                package_name=None,
                class_name=store_backend['class_name']
            )

        filepath_prefix = 'validations'
        validation_result_idendifier_obj = instantiate_class_from_config(
            config=store_backend,
            runtime_environment=runtime_environment,
            config_defaults={
                "module_name": module_name,
                "filepath_prefix": filepath_prefix,
                "filepath_suffix": filepath_suffix,
            }
        )
        if not validation_result_idendifier_obj:
            raise ClassInstantiationError(
                module_name=module_name,
                package_name=None,
                class_name=store_backend['class_name']
            )

        filepath_template = 'index.html'
        index_page_obj = instantiate_class_from_config(
            config=store_backend,
            runtime_environment=runtime_environment,
            config_defaults={
                "module_name": module_name,
                "filepath_template": filepath_template,
            }
        )
        if not index_page_obj:
            raise ClassInstantiationError(
                module_name=module_name,
                package_name=None,
                class_name=store_backend['class_name']
            )

        filepath_template = None
        static_assets_obj = instantiate_class_from_config(
            config=store_backend,
            runtime_environment=runtime_environment,
            config_defaults={
                "module_name": module_name,
                "filepath_template": filepath_template,
            }
        )
        if not static_assets_obj:
            raise ClassInstantiationError(
                module_name=module_name,
                package_name=None,
                class_name=store_backend['class_name']
            )

        self.store_backends = {
            ExpectationSuiteIdentifier: expectation_suite_identifier_obj,
            ValidationResultIdentifier: validation_result_idendifier_obj,
            "index_page": index_page_obj,
            "static_assets": static_assets_obj,
        }

        # NOTE: Instead of using the filesystem as the source of record for keys,
        # this class tracks keys separately in an internal set.
        # This means that keys are stored for a specific session, but can't be fetched after the original
        # HtmlSiteStore instance leaves scope.
        # Doing it this way allows us to prevent namespace collisions among keys while still having multiple
        # backends that write to the same directory structure.
        # It's a pretty reasonable way for HtmlSiteStore to do its job---you just ahve to remember that it
        # can't necessarily set and list_keys like most other Stores.
        self.keys = set()

    def get(self, key):
        self._validate_key(key)
        return self.store_backends[
            type(key.resource_identifier)
        ].get(key.to_tuple())

    def set(self, key, serialized_value):
        self._validate_key(key)
        self.keys.add(key)

        return self.store_backends[
            type(key.resource_identifier)
        ].set(key.resource_identifier.to_tuple(), serialized_value,
              content_encoding='utf-8', content_type='text/html; charset=utf-8')

    def get_url_for_resource(self, resource_identifier=None, only_if_exists=True):
        """
        Return the URL of the HTML document that renders a resource
        (e.g., an expectation suite or a validation result).

        :param resource_identifier: ExpectationSuiteIdentifier, ValidationResultIdentifier
                or any other type's identifier. The argument is optional - when
                not supplied, the method returns the URL of the index page.
        :return: URL (string)
        """
        if resource_identifier is None:
            store_backend = self.store_backends["index_page"]
            key = ()
        elif isinstance(resource_identifier, ExpectationSuiteIdentifier):
            store_backend = self.store_backends[ExpectationSuiteIdentifier]
            key = resource_identifier.to_tuple()
        elif isinstance(resource_identifier, ValidationResultIdentifier):
            store_backend = self.store_backends[ValidationResultIdentifier]
            key = resource_identifier.to_tuple()
        else:
            # this method does not support getting the URL of static assets
            raise ValueError("Cannot get URL for resource {0:s}".format(str(resource_identifier)))

        if only_if_exists:
            return store_backend.get_url_for_key(key) if store_backend.has_key(key) else None
        return store_backend.get_url_for_key(key)

    def _validate_key(self, key):
        if not isinstance(key, SiteSectionIdentifier):
            raise TypeError("key: {!r} must a SiteSectionIdentifier, not {!r}".format(
                key,
                type(key),
            ))

        for key_class in self.store_backends.keys():
            try:
                if isinstance(key.resource_identifier, key_class):
                    return
            except TypeError:
                # it's ok to have a key that is not a type (e.g. the string "index_page")
                continue

        # The key's resource_identifier didn't match any known key_class
        raise TypeError("resource_identifier in key: {!r} must one of {}, not {!r}".format(
            key,
            set(self.store_backends.keys()),
            type(key),
        ))

    def list_keys(self):
        keys = []
        for type_, backend in self.store_backends.items():
            try:
                # If the store_backend does not support list_keys...
                key_tuples = backend.list_keys()
            except NotImplementedError:
                pass
            try:
                if issubclass(type_, DataContextKey):
                    keys += [type_.from_tuple(tuple_) for tuple_ in key_tuples]
            except TypeError:
                # If the key in store_backends is not itself a type...
                pass
        return keys

    def write_index_page(self, page):
        """This third param_store has a special method, which uses a zero-length tuple as a key."""
        return self.store_backends["index_page"].set((), page, content_encoding='utf-8', content_type='text/html; '
                                                                                                      'charset=utf-8')

    def clean_site(self):
        for _, target_store_backend in self.store_backends.items():
            keys = target_store_backend.list_keys()
            for key in keys:
                target_store_backend.remove_key(key)

    def copy_static_assets(self, static_assets_source_dir=None):
        """
        Copies static assets, using a special "static_assets" backend store that accepts variable-length tuples as
        keys, with no filepath_template.
        """
        file_exclusions = [".DS_Store"]
        dir_exclusions = []

        if not static_assets_source_dir:
            static_assets_source_dir = file_relative_path(__file__, os.path.join("..", "..", "render", "view", "static"))

        for item in os.listdir(static_assets_source_dir):
            # Directory
            if os.path.isdir(os.path.join(static_assets_source_dir, item)):
                if item in dir_exclusions:
                    continue
                # Recurse
                new_source_dir = os.path.join(static_assets_source_dir, item)
                self.copy_static_assets(new_source_dir)
            # File
            else:
                # Copy file over using static assets store backend
                if item in file_exclusions:
                    continue
                source_name = os.path.join(static_assets_source_dir, item)
                with open(source_name, 'rb') as f:
                    # Only use path elements starting from static/ for key
                    store_key = tuple(os.path.normpath(source_name).split(os.sep))
                    store_key = store_key[store_key.index('static'):]
                    content_type, content_encoding = guess_type(item, strict=False)

                    if content_type is None:
                        # Use GE-known content-type if possible
                        if source_name.endswith(".otf"):
                            content_type = "font/opentype"
                        else:
                            # fallback
                            logger.warning("Unable to automatically determine content_type for {}".format(source_name))
                            content_type = "text/html; charset=utf8"

                    self.store_backends["static_assets"].set(
                        store_key,
                        f.read(),
                        content_encoding=content_encoding,
                        content_type=content_type
                    )
