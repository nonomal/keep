import datetime
import json
import logging
import os
from typing import Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, text

from keep.api.core.cel_to_sql.ast_nodes import DataType
from keep.api.core.cel_to_sql.properties_metadata import (
    FieldMappingConfiguration,
    PropertiesMetadata,
    PropertyMetadataInfo,
)
from keep.api.core.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
    get_cel_to_sql_provider,
)
from keep.api.core.db import engine

# This import is required to create the tables
from keep.api.core.facets import get_facet_options, get_facets
from keep.api.models.alert import AlertSeverity, AlertStatus
from keep.api.models.db.alert import (
    Alert,
    AlertEnrichment,
    AlertField,
    Incident,
    LastAlert,
    LastAlertToIncident,
)
from keep.api.models.db.facet import FacetType
from keep.api.models.db.incident import IncidentStatus
from keep.api.models.facet import FacetDto, FacetOptionDto, FacetOptionsQueryDto
from keep.api.models.query import QueryDto, SortOptionsDto

logger = logging.getLogger(__name__)

alerts_hard_limit = int(os.environ.get("KEEP_LAST_ALERTS_LIMIT", 50000))

alert_field_configurations = [
    FieldMappingConfiguration(
        map_from_pattern="id", map_to="lastalert.alert_id", data_type=DataType.UUID
    ),
    FieldMappingConfiguration(
        map_from_pattern="source",
        map_to="alert.provider_type",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="providerId",
        map_to="alert.provider_id",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="providerType",
        map_to="alert.provider_type",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="timestamp",
        map_to="lastalert.timestamp",
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="fingerprint",
        map_to="lastalert.fingerprint",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="startedAt",
        map_to="lastalert.first_timestamp",
        data_type=DataType.DATETIME
    ),
    FieldMappingConfiguration(
        map_from_pattern="incident.id",
        map_to=[
            "incident.id",
        ],
        data_type=DataType.UUID,
    ),
    FieldMappingConfiguration(
        map_from_pattern="incident.name",
        map_to=[
            "incident.user_generated_name",
            "incident.ai_generated_name",
        ],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="severity",
        map_to=[
            "JSON(alertenrichment.enrichments).*",
            "JSON(alert.event).*",
        ],
        enum_values=[
            severity.value
            for severity in sorted(
                [severity for _, severity in enumerate(AlertSeverity)],
                key=lambda s: s.order,
            )
        ],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="lastReceived",
        map_to=[
            "JSON(alertenrichment.enrichments).*",
            "JSON(alert.event).*",
        ],
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="status",
        map_to=[
            "JSON(alertenrichment.enrichments).*",
            "JSON(alert.event).*",
        ],
        enum_values=list(reversed([item.value for _, item in enumerate(AlertStatus)])),
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="dismissed",
        map_to=["JSON(alertenrichment.enrichments).*"],
        data_type=DataType.BOOLEAN,
    ),
    FieldMappingConfiguration(
        map_from_pattern="firingCounter",
        map_to=[
            "JSON(alertenrichment.enrichments).*",
            "JSON(alert.event).*",
        ],
        data_type=DataType.INTEGER,
    ),
    FieldMappingConfiguration(
        map_from_pattern="*",
        map_to=[
            "JSON(alertenrichment.enrichments).*",
            "JSON(alert.event).*",
        ],
        data_type=DataType.STRING,
    ),
]

# Copies the same configuration as above, but adds the "alert." prefix to each entry in map_from_pattern.
# This allows users to write queries using dictionary-style field access, like:
#   alert['some_attribute'] == 'value'
field_configurations_with_alert_prefix = []
for item in alert_field_configurations:
    field_configurations_with_alert_prefix.append(
        FieldMappingConfiguration(
            map_from_pattern=f"alert.{item.map_from_pattern}",
            map_to=item.map_to,
            data_type=item.data_type,
            enum_values=item.enum_values,
        )
    )
alert_field_configurations = (
    field_configurations_with_alert_prefix + alert_field_configurations
)

properties_metadata = PropertiesMetadata(alert_field_configurations)

static_facets = [
    FacetDto(
        id="f8a91ac7-4916-4ad0-9b46-a5ddb85bfbb8",
        property_path="severity",
        name="Severity",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="5dd1519c-6277-4109-ad95-c19d2f4f15e3",
        property_path="status",
        name="Status",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="461bef05-fc20-4363-b427-9d26fe064e7f",
        property_path="source",
        name="Source",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="6afa12d7-21df-4694-8566-fd56d5ee2266",
        property_path="incident.name",
        name="Incident",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="77b8a6d4-3b8d-4b6a-9f8e-2c8e4b8f8e4c",
        property_path="dismissed",
        name="Dismissed",
        is_static=True,
        type=FacetType.str,
    ),
]
static_facets_dict = {facet.id: facet for facet in static_facets}


def get_threeshold_query(tenant_id: str):
    return func.coalesce(
        select(LastAlert.timestamp)
        .select_from(LastAlert)
        .where(LastAlert.tenant_id == tenant_id)
        .order_by(LastAlert.timestamp.desc())
        .limit(1)
        .offset(alerts_hard_limit - 1)
        .scalar_subquery(),
        datetime.datetime.min,
    )


def __build_query_for_filtering(
    tenant_id: str,
    select_args: list,
    cel=None,
    limit=None,
    fetch_alerts_data=True,
    fetch_incidents=False,
    force_fetch=False,
):
    fetch_incidents = fetch_incidents or (cel and "incident." in cel)
    cel_to_sql_instance = get_cel_to_sql_provider(properties_metadata)
    sql_filter = None
    involved_fields = []

    if cel:
        cel_to_sql_result = cel_to_sql_instance.convert_to_sql_str_v2(cel)
        sql_filter = cel_to_sql_result.sql
        involved_fields = cel_to_sql_result.involved_fields
        fetch_incidents = next(
            (
                True
                for field in involved_fields
                if field.field_name.startswith("incident.")
            ),
            False,
        )

    sql_query = select(*select_args).select_from(LastAlert)

    if fetch_alerts_data or force_fetch:
        sql_query = sql_query.join(
            Alert,
            and_(
                Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id
            ),
        ).outerjoin(
            AlertEnrichment,
            and_(
                LastAlert.tenant_id == AlertEnrichment.tenant_id,
                LastAlert.fingerprint == AlertEnrichment.alert_fingerprint,
            ),
        )

    if fetch_incidents or force_fetch:
        sql_query = sql_query.outerjoin(
            LastAlertToIncident,
            and_(
                LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                LastAlert.fingerprint == LastAlertToIncident.fingerprint,
            ),
        ).outerjoin(
            Incident,
            and_(
                LastAlertToIncident.tenant_id == Incident.tenant_id,
                LastAlertToIncident.incident_id == Incident.id,
                Incident.status == IncidentStatus.FIRING.value,
            ),
        )

    sql_query = sql_query.filter(LastAlert.tenant_id == tenant_id).filter(
        LastAlert.timestamp >= get_threeshold_query(tenant_id)
    )
    involved_fields = []

    if sql_filter:
        sql_query = sql_query.where(text(sql_filter))
    return {
        "query": sql_query,
        "involved_fields": involved_fields,
        "fetch_incidents": fetch_incidents,
    }


def build_total_alerts_query(tenant_id, query: QueryDto):
    fetch_incidents = query.cel and "incident." in query.cel
    fetch_alerts_data = query.cel is not None or query.cel != ""

    count_funct = (
        func.count(func.distinct(LastAlert.alert_id))
        if fetch_incidents
        else func.count(1)
    )
    built_query_result = __build_query_for_filtering(
        tenant_id=tenant_id,
        cel=query.cel,
        select_args=[count_funct],
        limit=query.limit,
        fetch_alerts_data=fetch_alerts_data,
    )

    return built_query_result["query"]


def build_alerts_query(tenant_id, query: QueryDto):
    cel_to_sql_instance = get_cel_to_sql_provider(properties_metadata)
    sort_by_exp = cel_to_sql_instance.get_order_by_expression(
        [
            (sort_option.sort_by, sort_option.sort_dir)
            for sort_option in query.sort_options
        ]
    )
    distinct_columns = [
        text(cel_to_sql_instance.get_field_expression(sort_option.sort_by))
        for sort_option in query.sort_options
    ]

    built_query_result = __build_query_for_filtering(
        tenant_id,
        select_args=[
            Alert,
            AlertEnrichment,
            LastAlert.first_timestamp.label("startedAt"),
        ]
        + distinct_columns,
        cel=query.cel,
    )
    sql_query = built_query_result["query"]
    fetch_incidents = built_query_result["fetch_incidents"]
    sql_query = sql_query.order_by(text(sort_by_exp))

    if fetch_incidents:
        sql_query = sql_query.distinct(*(distinct_columns + [Alert.id]))

    if query.limit is not None:
        sql_query = sql_query.limit(query.limit)

    if query.offset is not None:
        sql_query = sql_query.offset(query.offset)

    return sql_query


def query_last_alerts(tenant_id, query: QueryDto) -> Tuple[list[Alert], int]:
    query_with_defaults = query.copy()

    # Shahar: this happens when the frontend query builder fails to build a query
    if query_with_defaults.cel == "1 == 1":
        logger.warning("Failed to build query for alerts")
        query_with_defaults.cel = ""
    if query_with_defaults.limit is None:
        query_with_defaults.limit = 1000
    if query_with_defaults.offset is None:
        query_with_defaults.offset = 0
    if query_with_defaults.sort_by is not None:
        query_with_defaults.sort_options = [
            SortOptionsDto(
                sort_by=query_with_defaults.sort_by,
                sort_dir=query_with_defaults.sort_dir,
            )
        ]
    if not query_with_defaults.sort_options:
        query_with_defaults.sort_options = [
            SortOptionsDto(sort_by="timestamp", sort_dir="desc")
        ]

    with Session(engine) as session:
        try:
            total_count_query = build_total_alerts_query(
                tenant_id=tenant_id, query=query_with_defaults
            )
            total_count = session.exec(total_count_query).one()[0]

            if not query_with_defaults.limit:
                return [], total_count

            if query_with_defaults.offset >= alerts_hard_limit:
                return [], total_count

            if (
                query_with_defaults.offset + query_with_defaults.limit
                > alerts_hard_limit
            ):
                query_with_defaults.limit = (
                    alerts_hard_limit - query_with_defaults.offset
                )

            data_query = build_alerts_query(tenant_id, query_with_defaults)
            alerts_with_start = session.execute(data_query).all()
        except OperationalError as e:
            logger.warning(
                f"Failed to query alerts for query object '{json.dumps(query_with_defaults.dict(exclude_unset=True))}': {e}"
            )
            return [], 0

        # Process results based on dialect
        alerts = []
        for alert_data in alerts_with_start:
            alert: Alert = alert_data[0]
            alert.alert_enrichment = alert_data[1]
            if not alert.event.get("startedAt"):
                alert.event["startedAt"] = str(alert_data[2])
            else:
                alert.event["firstTimestamp"] = str(alert_data[2])
            alert.event["event_id"] = str(alert.id)
            alerts.append(alert)

        return alerts, total_count


def get_alert_facets_data(
    tenant_id: str,
    facet_options_query: FacetOptionsQueryDto,
) -> dict[str, list[FacetOptionDto]]:
    if facet_options_query and facet_options_query.facet_queries:
        facets = get_alert_facets(tenant_id, facet_options_query.facet_queries.keys())
    else:
        facets = static_facets

    def base_query_factory(
        facet_property_path: str,
        involved_fields: PropertyMetadataInfo,
        select_statement,
    ):
        fetch_incidents = "incident." in facet_property_path or next(
            (True for item in involved_fields if "incident." in item.field_name),
            False,
        )
        return __build_query_for_filtering(
            tenant_id=tenant_id,
            select_args=select_statement,
            force_fetch=False,
            fetch_incidents=fetch_incidents,
        )["query"]

    return get_facet_options(
        base_query_factory=base_query_factory,
        entity_id_column=LastAlert.alert_id,
        facets=facets,
        facet_options_query=facet_options_query,
        properties_metadata=properties_metadata,
    )


def get_alert_facets(
    tenant_id: str, facet_ids_to_load: list[str] = None
) -> list[FacetDto]:
    not_static_facet_ids = []
    facets = []

    if not facet_ids_to_load:
        return static_facets + get_facets(tenant_id, "alert")

    if facet_ids_to_load:
        for facet_id in facet_ids_to_load:
            if facet_id not in static_facets_dict:
                not_static_facet_ids.append(facet_id)
                continue

            facets.append(static_facets_dict[facet_id])

    if not_static_facet_ids:
        facets += get_facets(tenant_id, "alert", not_static_facet_ids)

    return facets


def get_alert_potential_facet_fields(tenant_id: str) -> list[str]:
    with Session(engine) as session:
        query = (
            select(AlertField.field_name)
            .select_from(AlertField)
            .where(AlertField.tenant_id == tenant_id)
            .distinct(AlertField.field_name)
        )
        result = session.exec(query).all()
        return [row[0] for row in result]
