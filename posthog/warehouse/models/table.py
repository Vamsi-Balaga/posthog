from posthog.models.utils import UUIDModel, CreatedMetaFields, sane_repr, DeletedMetaFields
from posthog.errors import wrap_query_error
from django.db import models
from posthog.models.team import Team
from posthog.client import sync_execute
from .credential import DataWarehouseCredential
from posthog.hogql.database.models import (
    StringDatabaseField,
    IntegerDatabaseField,
    DateTimeDatabaseField,
    StringJSONDatabaseField,
    BooleanDatabaseField,
)
from posthog.hogql.database.s3_table import S3Table
import re

ClickhouseHogqlMapping = {
    "String": StringDatabaseField,
    "DateTime64": DateTimeDatabaseField,
    "DateTime32": DateTimeDatabaseField,
    "UInt8": IntegerDatabaseField,
    "UInt16": IntegerDatabaseField,
    "UInt32": IntegerDatabaseField,
    "UInt64": IntegerDatabaseField,
    "Float8": IntegerDatabaseField,
    "Float16": IntegerDatabaseField,
    "Float32": IntegerDatabaseField,
    "Float64": IntegerDatabaseField,
    "Int32": IntegerDatabaseField,
    "Int64": IntegerDatabaseField,
    "Tuple": StringJSONDatabaseField,
    "Array": StringJSONDatabaseField,
    "Map": StringJSONDatabaseField,
    "Bool": BooleanDatabaseField,
}

ExtractErrors = {
    "The AWS Access Key Id you provided does not exist": "The Access Key you provided does not exist",
}


class DataWarehouseTable(CreatedMetaFields, UUIDModel, DeletedMetaFields):
    class TableFormat(models.TextChoices):
        CSV = "CSV", "CSV"
        Parquet = "Parquet", "Parquet"

    name: models.CharField = models.CharField(max_length=128)
    format: models.CharField = models.CharField(max_length=128, choices=TableFormat.choices)
    team: models.ForeignKey = models.ForeignKey(Team, on_delete=models.CASCADE)

    url_pattern: models.CharField = models.CharField(max_length=500)
    credential: models.ForeignKey = models.ForeignKey(
        DataWarehouseCredential, on_delete=models.CASCADE, null=True, blank=True
    )

    columns: models.JSONField = models.JSONField(
        default=dict, null=True, blank=True, help_text="Dict of all columns with Clickhouse type (including Nullable())"
    )

    __repr__ = sane_repr("name")

    def get_columns(self):
        try:
            result = sync_execute(
                """DESCRIBE TABLE (
                SELECT * FROM
                    s3Cluster('posthog', %(url_pattern)s, %(access_key)s, %(access_secret)s, %(format)s)
                LIMIT 1
            )""",
                {
                    "url_pattern": self.url_pattern,
                    "access_key": self.credential.access_key,
                    "access_secret": self.credential.access_secret,
                    "format": self.format,
                },
            )
        except Exception as err:
            self._safe_expose_ch_error(err)
        return {item[0]: item[1] for item in result}

    def hogql_definition(self) -> S3Table:
        if not self.columns:
            raise Exception("Columns must be fetched and saved to use in HogQL.")

        fields = {}
        structure = []
        for column, type in self.columns.items():
            if type.startswith("Nullable("):
                type = type.replace("Nullable(", "")[:-1]

            # TODO: remove when addressed https://github.com/ClickHouse/ClickHouse/issues/37594
            if type.startswith("Array("):
                type = self.remove_named_tuples(type)

            structure.append(f"{column} {type}")
            type = type.partition("(")[0]
            type = ClickhouseHogqlMapping[type]
            fields[column] = type(name=column)

        return S3Table(
            name=self.name,
            url=self.url_pattern,
            format=self.format,
            access_key=self.credential.access_key,
            access_secret=self.credential.access_secret,
            fields=fields,
            structure=", ".join(structure),
        )

    def remove_named_tuples(self, type):
        """Remove named tuples from query"""
        tokenified_type = re.split(r"(\W)", type)
        filtered_tokens = [
            token
            for token in tokenified_type
            if token == "Nullable"
            or (len(token) == 1 and not token.isalnum())
            or token in ClickhouseHogqlMapping.keys()
        ]
        return "".join(filtered_tokens)

    def _safe_expose_ch_error(self, err):
        err = wrap_query_error(err)
        for key, value in ExtractErrors.items():
            if key in err.message:
                raise Exception(value)
        raise Exception("Could not get columns")
