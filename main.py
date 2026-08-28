#!/usr/bin/python
# -*- coding:utf-8 -*-
# Filename: main.py

import argparse
import logging
import os
import sys
from typing import Any, Sequence

from envyaml import EnvYAML
from prometheus_client import start_http_server

from app.exporter import MetricExporter


class KeyValueArg(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, dict())

        if values:
            for kvpair in values:
                assert len(kvpair.split("=")) == 2

                key, value = kvpair.split("=")
                getattr(namespace, self.dest)[key] = value


def get_configs() -> EnvYAML:
    parser = argparse.ArgumentParser(
        description="Azure Cost Exporter, exposing Azure cost data as Prometheus metrics."
    )
    parser.add_argument(
        "-c", "--config", required=True, help="The config file (exporter_config.yaml) for the exporter"
    )
    args = parser.parse_args()

    if not os.path.exists(args.config) or not os.path.isfile(args.config):
        logging.error(f"Config file '{args.config}' does not exist, or it is not a file!")
        sys.exit(1)

    config = EnvYAML(args.config)

    # config validation
    if len(config["target_azure_accounts"]) == 0:
        logging.error("There should be at least one target Azure accounts defined in the config!")
        sys.exit(1)

    labels = {"TenantId", "Subscription", "ProjectName", "EnvironmentName", "ClientId", "ClientSecret"}

    for idx, tenant in enumerate(config["target_azure_accounts"]):
        if set(tenant.keys()) != labels:
            logging.error(
                f"The target in position {idx + 1} Azure accounts have missing keys (should be: {labels} found {tenant.keys()})!"
            )
            sys.exit(1)
        if tenant["ClientId"] == "PUT_YOUR_AZURE_CLIENT_ID_HERE" or len(tenant["ClientId"]) == 0:
            logging.error(f"ClientId is missing in target Azure account {tenant['TenantId']}!")
            sys.exit(1)
        if tenant["ClientSecret"] == "PUT_YOUR_AZURE_CLIENT_SECRET_HERE" or len(tenant["ClientSecret"]) == 0:
            logging.error(f"ClientSecret is missing in target Azure account {tenant['TenantId']}!")
            sys.exit(1)
    return config


def main(conf: EnvYAML | dict[str, Any]) -> None:
    app_metrics = MetricExporter(
        polling_interval_seconds=conf["polling_interval_seconds"],
        metric_name=conf["metric_name"],
        metric_name_usd=conf["metric_name_usd"],
        group_by=conf["group_by"],
        targets=conf["target_azure_accounts"],
    )
    start_http_server(conf["exporter_port"])
    app_metrics.run_metrics_loop()


if __name__ == "__main__":
    logger_format = "%(asctime)-15s %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s"
    default_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, default_log_level, logging.INFO), format=logger_format)

    # Silenzia i log HTTP e di autenticazione verbosi dell'SDK Azure e urllib3
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    config = get_configs()
    if "log_level" in config and config["log_level"]:
        log_level_str = str(config["log_level"]).upper()
        logging.getLogger().setLevel(getattr(logging, log_level_str, logging.INFO))
        logging.getLogger("azure").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    main(config)
