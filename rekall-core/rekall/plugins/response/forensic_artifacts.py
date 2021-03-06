# Rekall Memory Forensics
# Copyright 2016 Google Inc. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""This module implements plugins related to forensic artifacts.

https://github.com/ForensicArtifacts
"""

__author__ = "Michael Cohen <scudette@google.com>"
import platform
import sys

import yaml

from artifacts import definitions
from artifacts import errors

from rekall import plugin
from rekall import obj
from rekall.ui import text
from rekall.ui import json_renderer

from rekall.plugins.response import common


class ArtifactResult(object):
    """Bundle all the results from an artifact."""
    def __init__(self, artifact_name=None, result_type=None, fields=None):
        self.artifact_name = artifact_name
        self.result_type = result_type
        self.results = []
        self.fields = fields or []

    def add_result(self, **data):
        if data:
            self.results.append(data)

    def merge(self, other):
        self.results.extend(other)


# Rekall defines a new artifact type.
TYPE_INDICATOR_REKALL = "REKALL_EFILTER"


class _FieldDefinitionValidator(object):
    """Loads and validates fields in a dict.

    We check their name, types and if they are optional according to a template
    in _field_definitions.
    """
    _field_definitions = []

    def _LoadFieldDefinitions(self, data):
        for field in self._field_definitions:
            name = field["name"]
            default = field.get("default")
            required_type = field.get("type")

            if required_type in (str, unicode):
                required_type = basestring

            if default is None and required_type is not None:
                # basestring cant be instantiated.
                if required_type is basestring:
                    default = ""
                else:
                    default = required_type()

            if required_type is None and default is not None:
                required_type = type(default)

            if not field.get("optional"):
                if name not in data:
                    raise errors.FormatError(
                        u'Missing fields {}.'.format(name))

            value = data.get(name, default)
            if default is not None and not isinstance(value, required_type):
                raise errors.FormatError(
                    u'field {} has type {} should be {}.'.format(
                        name, type(data[name]), required_type))

            if field.get("checker"):
                value = field["checker"](self, data)

            setattr(self, name, value)


class SourceType(_FieldDefinitionValidator):
    """All sources inherit from this."""

    def __init__(self, source_definition):
        attributes = source_definition["attributes"]
        self.type_indicator = source_definition["type"]
        self._LoadFieldDefinitions(attributes)


    def apply(self, artifact_name=None, fields=None, result_type=None, **_):
        """Generate ArtifactResult instances."""
        return ArtifactResult(artifact_name=artifact_name,
                              result_type=result_type,
                              fields=fields)


class RekallEFilterArtifacts(SourceType):
    """Class to support Rekall Efilter artifact types."""

    allowed_types = {
        "int": int,
        "unicode": unicode,  # Unicode data.
        "str": str, # Used for binary data.
        "float": float,
        "any": str  # Used for opaque types that can not be further processed.
    }

    _field_definitions = [
        dict(name="query", type=basestring),
        dict(name="query_parameters", default=[], optional=True),
        dict(name="fields", type=list),
        dict(name="type_name", type=basestring),
        dict(name="supported_os", optional=True,
             default=definitions.SUPPORTED_OS),
    ]

    def __init__(self, source_definition):
        super(RekallEFilterArtifacts, self).__init__(source_definition)
        for column in self.fields:
            if "name" not in column or "type" not in column:
                raise errors.FormatError(
                    u"Field definition should have both name and type.")

            mapped_type = column["type"]
            if mapped_type not in self.allowed_types:
                raise errors.FormatError(
                    u"Unsupported type %s." % mapped_type)

    def apply(self, session=None, **kwargs):
        result = super(RekallEFilterArtifacts, self).apply(
            fields=self.fields, result_type=self.type_name, **kwargs)

        search = session.plugins.search(
            query=self.query,
            query_parameters=self.query_parameters)

        for match in search.solve():
            row = {}
            for column in self.fields:
                name = column["name"]
                type = column["type"]
                value = match.get(name)
                if value is None:
                    continue

                row[name] = RekallEFilterArtifacts.allowed_types[
                    type](value)

            result.add_result(**row)

        yield result


class FileSourceType(SourceType):
    _field_definitions = [
        dict(name="paths", default=[]),
        dict(name="separator", default="/", type=basestring,
             optional=True),
        dict(name="supported_os", optional=True,
             default=definitions.SUPPORTED_OS),
    ]

    # These fields will be present in the ArtifactResult object we return.
    _FIELDS = [
        dict(name="st_mode", type="unicode"),
        dict(name="st_nlink", type="int"),
        dict(name="st_uid", type="unicode"),
        dict(name="st_gid", type="unicode"),
        dict(name="st_size", type="int"),
        dict(name="st_mtime", type="unicode"),
        dict(name="filename", type="unicode"),
    ]

    def apply(self, session=None, **kwargs):
        result = super(FileSourceType, self).apply(
            fields=self._FIELDS, result_type="file_information", **kwargs)

        for hits in session.plugins.glob(self.paths).collect():
            # Hits are FileInformation objects, and we just pick some of the
            # important fields to report.
            info = hits["path"]
            row = {}
            for field in self._FIELDS:
                name = field["name"]
                type = RekallEFilterArtifacts.allowed_types[field["type"]]
                row[name] = type(info[name])

            result.add_result(**row)

        yield result


class ArtifactGroupSourceType(SourceType):
    _field_definitions = [
        dict(name="names", type=list),
        dict(name="supported_os", optional=True,
             default=definitions.SUPPORTED_OS),
    ]

    def apply(self, collector=None, **_):
        for name in self.names:
            for result in collector.collect_artifact(name):
                yield result


# This lookup table maps between source type name and concrete implementations
# that we support. Artifacts which contain sources which are not implemented
# will be ignored.
SOURCE_TYPES = {
    TYPE_INDICATOR_REKALL: RekallEFilterArtifacts,
    definitions.TYPE_INDICATOR_FILE: FileSourceType,
    definitions.TYPE_INDICATOR_ARTIFACT_GROUP: ArtifactGroupSourceType,
}


class ArtifactDefinition(_FieldDefinitionValidator):
    """The main artifact class."""

    def CheckLabels(self, art_definition):
        """Ensure labels are defined."""
        labels = art_definition.get("labels", [])
        # Keep unknown labels around in case callers want to check for complete
        # label coverage. In most cases it is desirable to allow users to extend
        # labels but when super strict validation is required we want to make
        # sure that users dont typo a label.
        self.undefined_labels = set(labels).difference(definitions.LABELS)
        return labels

    def BuildSources(self, art_definition):
        sources = art_definition["sources"]
        result = []
        self.unsupported_source_types = []
        for source in sources:
            if not isinstance(source, dict):
                raise errors.FormatError("Source is not a dict.")

            source_type_name = source.get("type")
            if source_type_name is None:
                raise errors.FormatError("Source has no type.")

            source_cls = SOURCE_TYPES.get(source_type_name)
            if source_cls:
                result.append(source_cls(source))
            else:
                self.unsupported_source_types.append(source_type_name)

        if not result:
            if self.unsupported_source_types:
                raise errors.FormatError(
                    "No supported sources: %s" % (
                        self.unsupported_source_types,))

            raise errors.FormatError("No available sources.")

        return result

    def SupportedOS(self, art_definition):
        supported_os = art_definition.get(
            "supported_os", definitions.SUPPORTED_OS)

        undefined_supported_os = set(supported_os).difference(
            definitions.SUPPORTED_OS)

        if undefined_supported_os:
            raise errors.FormatError(
                u'supported operating system: {} '
                u'not defined.'.format(
                    u', '.join(undefined_supported_os)))

        return supported_os

    _field_definitions = [
        dict(name="name", type=basestring),
        dict(name="doc", type=basestring),
        dict(name="labels", default=[],
             checker=CheckLabels, optional=True),
        dict(name="sources", default=[],
             checker=BuildSources),
        dict(name="supported_os",
             checker=SupportedOS, optional=True),
        dict(name="conditions", default=[], optional=True),
        dict(name="returned_types", default=[], optional=True),
        dict(name="provides", type=list, optional=True),
        dict(name="urls", type=list, optional=True)
    ]

    name = "unknown"

    def __init__(self, data):
        try:
            self._LoadDefinition(data)
        except Exception as e:
            exc_info = sys.exc_info()
            raise errors.FormatError(
                "Definition %s: %s" % (self.name, e)), None, exc_info[2]

    def _LoadDefinition(self, data):
        if not isinstance(data, dict):
            raise errors.FormatError(
                "Artifact definition must be a dict.")

        different_keys = set(data) - definitions.TOP_LEVEL_KEYS
        if different_keys:
            raise errors.FormatError(u'Undefined keys: {}'.format(
                different_keys))

        self._LoadFieldDefinitions(data)


class ArtifactDefinitionProfileSectionLoader(obj.ProfileSectionLoader):
    """Loads artifacts from the artifact profiles."""
    name = "$ARTIFACTS"

    def LoadIntoProfile(self, session, profile, art_definitions):
        for definition in art_definitions:
            try:
                profile.AddDefinition(definition)
            except errors.FormatError as e:
                session.logging.debug(
                    "Skipping Artifact %s: %s", definition.get("name"), e)

        return profile


class ArtifactProfile(obj.Profile):
    """A profile containing artifact definitions."""

    # This will contain the definitions.
    def __init__(self, *args, **kwargs):
        super(ArtifactProfile, self).__init__(*args, **kwargs)
        self.definitions = []
        self.definitions_by_name = {}

    def AddDefinition(self, definition):
        """Add a new definition from a dict."""
        artifact = ArtifactDefinition(definition)
        self.definitions.append(artifact)
        self.definitions_by_name[definition["name"]] = artifact

    def GetDefinitionByName(self, name):
        return self.definitions_by_name[name]

    def GetDefinitions(self):
        return self.definitions


class ArtifactsCollector(plugin.TypedProfileCommand,
                         plugin.Command):
    """Collects artifacts."""

    name = "artifact_collector"

    __args = [
        dict(name="artifacts", positional=True, required=True,
             type="ArrayStringParser",
             help="A list of artifact names to collect."),

        dict(name="artifact_files", type="ArrayStringParser",
             help="A list of additional yaml files to load which contain "
             "artifact definitions."),

        dict(name="definitions", type="ArrayStringParser",
             help="An inline artifact definition in yaml format.")
    ]

    table_header = [
        dict(name="Divider", cname="divider", type="Divider"),
        dict(name="Result", cname="result"),
    ]

    table_options = dict(
        suppress_headers=True
    )

    def column_types(self):
        return dict(path=common.FileInformation(filename="/etc"))

    def __init__(self, *args, **kwargs):
        super(ArtifactsCollector, self).__init__(*args, **kwargs)
        self.artifact_profile = self.session.LoadProfile("artifacts")

        # Make a copy of the artifact registry.
        if self.plugin_args.definitions:
            self.artifact_profile = self.artifact_profile.copy()

            for definition in self.plugin_args.definitions:
                for definition_data in yaml.safe_load_all(definition):
                    self.artifact_profile.AddDefinition(definition_data)

        self.seen = set()
        # Determine which context we are running in. If we are running in live
        # mode, we use the platform to determine the supported OS, otherwise we
        # determine it from the profile.
        if self.session.GetParameter("live"):
            self.supported_os = platform.system()
        elif self.session.profile.metadata("os") == "linux":
            self.supported_os = "Linux"

        elif self.session.profile.metadata("os") == "windows":
            self.supported_os = "Windows"

        elif self.session.profile.metadata("os") == "darwin":
            self.supported_os = "Darwin"
        else:
            raise plugin.PluginError(
                "Unable to determine running environment.")

    def _evaluate_conditions(self, conditions):
        # TODO: Implement an expression parser for these. For now we just return
        # True always.
        return True

    def collect_artifact(self, artifact_name):
        if artifact_name in self.seen:
            return

        self.seen.add(artifact_name)

        definition = self.artifact_profile.GetDefinitionByName(artifact_name)

        # This artifact is not for us.
        if self.supported_os not in definition.supported_os:
            self.session.logging.debug(
                "Skipping artifact %s: Supported OS: %s, but we are %s",
                definition.name, definition.supported_os,
                self.supported_os)
            return

        if not self._evaluate_conditions(definition.conditions):
            return

        yield dict(divider="Artifact: %s" % definition.name)

        for source in definition.sources:
            # This source is not for us.
            if self.supported_os not in source.supported_os:
                self.session.logging.debug(
                    "Skipping artifact %s: Supported OS: %s",
                    definition.name, definition.supported_os)
                return

            for result in source.apply(
                    artifact_name=definition.name,
                    session=self.session,
                    collector=self):
                if isinstance(result, dict):
                    yield result
                else:
                    yield dict(result=result)

    def collect(self):
        self.seen = set()

        for artifact_name in self.plugin_args.artifacts:
            for x in self.collect_artifact(artifact_name):
                yield x


class ArtifactsList(plugin.TypedProfileCommand,
                    plugin.Command):
    """List details about all known artifacts."""

    name = "artifact_list"

    __args = [
        dict(name="regex", type="RegEx",
             default=".",
             help="Filter the artifact name."),
        dict(name="supported_os", type="ArrayStringParser",
             default=[platform.system()],
             help="If specified show for these OSs."),
        dict(name="labels", type="ArrayStringParser",
             help="Filter by these labels.")
    ]

    table_header = [
        dict(name="Name", width=30),
        dict(name="OS", width=8),
        dict(name="Labels", width=20),
        dict(name="Types", width=20),
        dict(name="Description", width=50),
    ]

    def collect(self):
        supported_os = set(self.plugin_args.supported_os)
        for definition in self.session.LoadProfile(
                "artifacts").GetDefinitions():
            if not supported_os.intersection(definition.supported_os):
                continue

            # Determine the type:
            types = set()
            for source in definition.sources:
                types.add(source.type_indicator)

            if self.plugin_args.regex.match(definition.name):
                yield (definition.name, definition.supported_os,
                       definition.labels, sorted(types), definition.doc)


class ArtifactResult_TextObjectRenderer(text.TextObjectRenderer):
    renders_type = "ArtifactResult"

    def render_row(self, target, **_):
        column_names = [x["name"] for x in target.fields]
        table = text.TextTable(
            columns=target.fields,
            renderer=self.renderer,
            session=self.session)

        if not target.results:
            return text.Cell("")

        result = [
            text.JoinedCell(*[text.Cell(x) for x in column_names]),
            text.JoinedCell(*[text.Cell("-" * len(x)) for x in column_names])]

        for row in target.results:
            ordered_row = []
            for column in column_names:
                ordered_row.append(row.get(column))

            result.append(table.get_row(*ordered_row))

        result = text.StackedCell(*result)
        return result


class ArtifactResult_DataExportObjectRenderer(
        json_renderer.StateBasedObjectRenderer):
    renders_type = "ArtifactResult"
    renderers = ["DataExportRenderer"]

    def GetState(self, item, **_):
        return dict(artifact_name=item.artifact_name,
                    result_type=item.result_type,
                    fields=item.fields,
                    results=item.results)
