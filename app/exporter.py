#!/usr/bin/python
# -*- coding:utf-8 -*-
# Filename: exporter.py

import logging
import time
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryAggregation,
    QueryDataset,
    QueryDefinition,
    QueryTimePeriod, QueryGrouping, QueryResult,
)
from prometheus_client import Gauge


class MetricExporter:
    def __init__(
            self,
            polling_interval_seconds: int,
            metric_name: str,
            metric_name_usd: str,
            group_by: dict[str, Any],
            targets: list[dict[str, Any]],
    ) -> None:
        self.polling_interval_seconds: int = polling_interval_seconds
        self.metric_name: str = metric_name
        self.metric_name_usd: str = metric_name_usd
        self.group_by: dict[str, Any] = group_by
        self.targets: list[dict[str, Any]] = targets
        self.clients: dict[tuple[str, str], CostManagementClient] = {}
        # we have verified that there is at least one target
        self.labels: set[str] = set(targets[0].keys())
        # for now we only support exporting one type of cost (ActualCost)
        self.labels.add("ChargeType")
        self.labels.add("Currency")
        if group_by.get("enabled"):
            for group in group_by.get("groups", []):
                self.labels.add(group["label_name"])
        self.azure_daily_cost: Gauge = Gauge(
            self.metric_name, "Daily cost of an Azure account in billing currency", self.labels
        )
        self.azure_daily_cost_usd: Gauge = Gauge(
            self.metric_name_usd, "Daily cost of an Azure account in USD", self.labels
        )

    def run_metrics_loop(self) -> None:
        while True:
            # every time we clear up all the existing labels before setting new ones
            self.azure_daily_cost.clear()
            self.azure_daily_cost_usd.clear()

            self.fetch()
            time.sleep(self.polling_interval_seconds)

    def get_azure_client(self, tenant_id: str, client_id: str, client_secret: str) -> CostManagementClient:
        key = (tenant_id, client_id)
        if key not in self.clients:
            self.clients[key] = CostManagementClient(
                credential=ClientSecretCredential(
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            )
        return self.clients[key]

    def init_azure_client(self, tenant_id: str, client_id: str, client_secret: str) -> CostManagementClient:
        return self.get_azure_client(tenant_id, client_id, client_secret)

    def query_azure_cost_explorer(
            self,
            azure_client: CostManagementClient,
            subscription: str,
            group_by: dict[str, Any],
            start_date: datetime,
            end_date: datetime,
            max_retries: int = 5,
    ) -> QueryResult:
        scope = f"/subscriptions/{subscription}"

        groups: list[QueryGrouping] = []
        if group_by.get("enabled"):
            for group in group_by.get("groups", []):
                groups.append(QueryGrouping(type=group["type"], name=group["name"]))

        query = QueryDefinition(
            type="ActualCost",
            dataset=QueryDataset(
                granularity="Daily",
                aggregation={
                    "totalCost": QueryAggregation(name="Cost", function="Sum"),
                    "totalCostUSD": QueryAggregation(name="CostUSD", function="Sum"),
                },
                grouping=groups,
            ),
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=timezone.utc),
                to=datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc),
            ),
        )

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    logging.info(
                        f"Executing retry attempt {attempt}/{max_retries} for subscription {subscription}..."
                    )
                result = azure_client.query.usage(scope, query)
                if attempt > 0:
                    logging.info(
                        f"Query succeeded on retry attempt {attempt}/{max_retries} for subscription {subscription}"
                    )
                return result
            except HttpResponseError as e:
                if e.status_code == 429 and attempt < max_retries:
                    retry_after = 15 * (2 ** attempt)
                    headers = getattr(getattr(e, "response", None), "headers", {}) or {}

                    found_header_retry: int | None = None
                    for k, v in headers.items():
                        if "retry-after" in k.lower() and v:
                            try:
                                val = int(v)
                                if found_header_retry is None or val > found_header_retry:
                                    found_header_retry = val
                            except ValueError:
                                pass

                    if found_header_retry is not None:
                        # Aggiungiamo 1 secondo di margine per evitare corse con la finestra del rate limiter
                        retry_after = found_header_retry + 1

                    err_details = e.message or (e.response.text() if hasattr(e.response, "text") else str(e))
                    logging.warning(
                        f"Rate limit (429) received for subscription {subscription}. Reason: \"{str.replace(err_details, '\n', ' ')}\". Retrying in {retry_after} seconds (attempt {attempt + 1}/{max_retries})..."
                    )
                    time.sleep(retry_after)
                else:
                    raise

        raise RuntimeError(f"Exceeded max retries ({max_retries}) for subscription {subscription}")

    def expose_metrics(self, azure_account: dict[str, Any], result: list[Any]) -> None:
        sub_id = azure_account.get("Subscription") or azure_account.get("subscription_id", "")
        logging.info(f"Exposing metrics for account {sub_id}")
        cost = float(result[0])
        cost_usd = float(result[1])

        if not self.group_by.get("enabled"):
            self.azure_daily_cost.labels(**azure_account, ChargeType="ActualCost", Currency=result[3]).set(cost)
            self.azure_daily_cost_usd.labels(**azure_account, ChargeType="ActualCost", Currency="USD").set(cost_usd)
        else:
            merged_minor_cost = 0.0
            merged_minor_cost_usd = 0.0
            group_key_values: dict[str, Any] = {}
            groups = self.group_by.get("groups", [])
            for i in range(len(groups)):
                value = result[i + 3]
                group_key_values[groups[i]["label_name"]] = value

            merge_config = self.group_by.get("merge_minor_cost", {})
            if merge_config.get("enabled") and cost < merge_config.get("threshold", 0):
                merged_minor_cost += cost
                merged_minor_cost_usd += cost_usd
            else:
                self.azure_daily_cost.labels(
                    **azure_account,
                    **group_key_values,
                    ChargeType="ActualCost",
                    Currency=result[len(groups) + 3],
                ).set(cost)
                self.azure_daily_cost_usd.labels(
                    **azure_account, **group_key_values, ChargeType="ActualCost", Currency="USD"
                ).set(cost_usd)

            if merged_minor_cost > 0:
                for i in range(len(groups)):
                    group_key_values[groups[i]["label_name"]] = merge_config.get("tag_value", "other")
                self.azure_daily_cost.labels(**azure_account, **group_key_values, ChargeType="ActualCost").set(
                    merged_minor_cost
                )
                self.azure_daily_cost_usd.labels(**azure_account, **group_key_values, ChargeType="ActualCost").set(
                    merged_minor_cost_usd
                )

    def fetch(self) -> None:
        for azure_account in self.targets:
            logging.info(f"Querying cost data for Azure tenant {azure_account['TenantId']}")
            azure_client = self.get_azure_client(
                azure_account["TenantId"], azure_account["ClientId"], azure_account["ClientSecret"]
            )

            try:
                end_date = datetime.now(timezone.utc)
                start_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                cost_response = self.query_azure_cost_explorer(
                    azure_client, azure_account["Subscription"], self.group_by, start_date, end_date
                )
            except HttpResponseError as e:
                logging.error(f"Failed to query cost data for subscription {azure_account['Subscription']}: {e}")
                continue
            if cost_response.properties is None:
                continue

            rows = cost_response.properties.rows or []
            target_date_int = int(start_date.strftime("%Y%m%d"))
            matched_rows = 0
            for result in rows:
                if result[2] != target_date_int:
                    # it is possible that Azure returns cost data which is different than the specified date
                    # for example, the query time period is 2023-07-10 00:00:00+00:00 to 2023-07-11 00:00:00+00:00
                    # Azure still returns some records for date 2023-07-11
                    continue
                else:
                    self.expose_metrics(azure_account, result)
                    matched_rows += 1

            logging.info(
                f"Completed fetch for subscription {azure_account['Subscription']}: processed {matched_rows}/{len(rows)} matching cost records"
            )
